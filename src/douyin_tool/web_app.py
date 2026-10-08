# -*- coding: utf-8 -*-
"""Web 服务：把核心功能包成本地网页界面。

设计要点：
- 只绑 127.0.0.1，不对外网开放
- 下载任务跑在后台线程里（f2 是 asyncio，Flask 是同步，需要桥接）
- 任务进度存内存，前端轮询 /api/progress
- 目标链接只允许抖音域名，防 SSRF
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from flask import Blueprint, Flask, jsonify, render_template, request

from . import browser_login as bl
from . import config as cfg_mod
from . import console
from . import cookie_reader as ck
from . import core
from . import validation

web = Blueprint("web", __name__)

# ---------------------------------------------------------------------------
# ★ 禁止 WebView2 缓存本地页面 —— 这是「越用启动越慢」的根因（2026-10-03 实测定案）
# ---------------------------------------------------------------------------
# 现象：同一份 exe，9-30 冷启动「页面已渲染」= 1.31s，10-03 变成 4.34s。
# 定位：把 data/webview_profile 换成全新目录后，WebView2 首帧从 **3306ms 掉到 169ms**
#       （快 20 倍），重启第二次仍然是 169ms —— 不是缓存预热问题，是旧 profile
#       里的某个状态在拖慢 WebView2 的启动路径。
#
# 再往下挖，在旧 profile 里抓到元凶：
#     data/webview_profile/EBWebView/Default/Cache/Cache_Data/
#     → 53 个 f_* 文件，其中 27 个**大小完全一样（51568 字节）**，
#       打开一看全是 `<!DOCTYPE html><html lang="zh-CN"...` —— 也就是
#       douyin_tool/web/templates/index.html 自己的副本。
#
# 为什么会有几十份重复副本：每本启动的服务都绑一个**随机空闲端口**，
# 于是每次都是**新的 origin**（http://127.0.0.1:不同端口/），HTTP 磁盘缓存
# 就把它当成一个全新的条目各存一份。加上 Flask 的 render_template 默认
# **不发任何 Cache-Control 头**，WebView2 只能按启发式规则把页面缓存起来 ——
# 日积月累，缓存条目越多，WebView2 冷启动重建索引/校验的代价越大。
#
# 对策：给页面和静态资源明确发 no-store。代价为零（页面上只有几十 KB 的
# 本地 HTML/CSS/JS，本来也不该走缓存），收益是端到端启动时间稳定不退化。
@web.after_request
def _no_store(resp):
    # 只对页面/静态资源生效，API 本来就不该被缓存
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ---------------------------------------------------------------------------
# 任务管理
# ---------------------------------------------------------------------------
_TASKS: Dict[str, Dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()
_DOWNLOAD_SLOT = threading.Semaphore(1)
MAX_ACTIVE_TASKS = 20
MAX_FINISHED_TASKS = 100


class TaskQueueFull(core.DouyinError):
    pass


@web.errorhandler(OSError)
def persistence_error(exc):
    return jsonify({"ok": False, "message": "本地文件操作失败，请检查目录权限和磁盘空间。"}), 500


@web.before_request
def validate_json_body():
    if request.path.startswith("/api/"):
        if request.host.split(":", 1)[0].lower() not in ("127.0.0.1", "localhost"):
            return jsonify({"ok": False, "message": "仅允许从本机访问。"}), 403
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify({"ok": False, "message": "不允许其他网站操作本地程序。"}), 403
    if request.path.startswith("/api/") and request.method == "POST" and request.content_length:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "message": "请求内容必须是有效的 JSON 对象。"}), 400


def _new_task(kind: str, target: str, total_hint: int = 0) -> str:
    tid = uuid.uuid4().hex[:12]
    with _TASKS_LOCK:
        if sum(t["status"] in ("pending", "running") for t in _TASKS.values()) >= MAX_ACTIVE_TASKS:
            raise TaskQueueFull("任务队列已满，请等待现有任务完成后重试。")
        finished = sorted((t for t in _TASKS.values()
                           if t["status"] not in ("pending", "running")), key=lambda t: t["created"])
        for t in finished[:-MAX_FINISHED_TASKS]:
            _TASKS.pop(t["id"], None)
        _TASKS[tid] = {
            "id": tid,
            "kind": kind,           # "one" | "post"
            "target": target,
            "status": "pending",    # pending|running|done|failed|cancelled
            "current": 0,
            "total": total_hint,
            "message": "排队中",
            "created": time.time(),
            "result": None,
            "error": None,
            "stop_flag": False,
        }
    return tid


def _update(tid: str, **kw: Any) -> None:
    with _TASKS_LOCK:
        if tid in _TASKS:
            _TASKS[tid].update(kw)


def _get(tid: str) -> Optional[Dict[str, Any]]:
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        return dict(t) if t else None


def _should_stop(tid: str):
    def fn() -> bool:
        with _TASKS_LOCK:
            t = _TASKS.get(tid)
            return bool(t and t.get("stop_flag"))
    return fn


def _run_in_thread(tid: str, coro_factory) -> None:
    """在后台线程里跑协程，并把进度写回任务表。"""
    def worker() -> None:
        def on_progress(cur: int, total: int, msg: str) -> None:
            _update(tid, current=cur, total=total, message=msg)

        acquired = False
        try:
            # f2 的进度管理器、信号管理器等是全局单例；传输按任务串行执行。
            while not acquired:
                core._check_stop(_should_stop(tid))
                acquired = _DOWNLOAD_SLOT.acquire(timeout=0.1)
            core._check_stop(_should_stop(tid))
            _update(tid, status="running", message="开始...")
            result = asyncio.run(core.run_cancellable(coro_factory(on_progress), _should_stop(tid)))
            with _TASKS_LOCK:
                task = _TASKS[tid]
                if task["stop_flag"]:
                    task.update(status="cancelled", result=result, message="已取消")
                elif result.get("failed") and not result.get("download_count") and not result.get("skipped"):
                    task.update(status="failed", result=result, message="下载失败", error="没有成功下载的作品。")
                else:
                    task.update(status="done", result=result,
                                message="完成（部分作品失败）" if result.get("failed") else "完成")
        except core.DownloadCancelled:
            _update(tid, status="cancelled", message="已取消", error=None)
        except Exception as e:  # noqa: BLE001
            # 我们自己的 DouyinError（含 LinkParseError）文案已经是给用户看的，
            # 直接展示；其它未预期的异常才带上类型名，便于排查。
            if isinstance(e, core.DouyinError):
                msg = str(e)
            else:
                msg = f"{type(e).__name__}: {e}"
            _update(tid, status="failed", error=msg, message="失败")
        finally:
            if acquired:
                _DOWNLOAD_SLOT.release()

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# 生命周期：关掉页面 → 程序自己退出
#
# 为什么需要：
#   exe 用 --noconsole 打包，双击后没有窗口，用户能看见的只有浏览器标签页。
#   关掉标签页时 Flask 仍在后台跑，再双击一次 exe 就又多一份进程 ——
#   实测会累积成 4 份（8 个进程：每份 1 个引导进程 + 1 个实际进程）。
#   这里的机制保证「关掉页面 = 进程退出」，正常情况下最多只会有一份在跑。
#
# 三条退出条件（任一满足）：
#   1. 页面主动上报关闭 → 宽限 CLOSE_GRACE 秒后退出。
#      宽限是用来容忍「刷新」的：刷新同样会触发 pagehide，但新页面加载后
#      立刻发一次心跳，就把待退出状态取消了。
#   2. 曾经有过页面连接，但心跳断了 IDLE_TIMEOUT 秒 → 浏览器崩溃/被强杀。
#      这个超时必须给得很宽：浏览器会节流后台标签页的定时器，甚至把页面
#      冻住（Chrome 的内存节省/省电模式），此时心跳会长时间不来 ——
#      但用户其实还开着页面。宁可让崩溃后的进程多活一会儿，也不能把
#      正在用的实例误杀。页面回到前台时会立刻补一次心跳。
#   3. 启动后 FIRST_SEEN_TIMEOUT 秒始终没人打开页面 → 浏览器没起来。
# ---------------------------------------------------------------------------
APP_ID = "douyin-tool"
APP_TITLE = "抖音视频下载器"

_LIFE: Dict[str, Any] = {
    "started": time.time(),
    "last_beat": None,    # 最近一次心跳时刻
    "close_at": None,     # 最近一次「页面已关闭」上报时刻
    "seen": False,        # 是否有过页面连上来
    "beats": 0,           # 心跳累计次数（诊断用：为 0 说明前端没接上）
    "exiting": False,     # 防止重复触发退出
    "mode": "browser",    # "window" = 装在自己的无边框窗口里；"browser" = 系统浏览器
}
_LIFE_LOCK = threading.Lock()

CLOSE_GRACE = 4.0            # 页面关闭后的宽限期（秒）
IDLE_TIMEOUT = 900.0         # 心跳断多久算浏览器没了
FIRST_SEEN_TIMEOUT = 300.0   # 启动后多久还没人打开页面
DRAIN_TIMEOUT = 20.0         # 退出前等正在跑的任务自然收尾
STOP_TIMEOUT = 12.0          # 等不动了就置 stop_flag，再等这么久
WATCHDOG_TICK = 1.0
SUSPEND_GAP = 30.0           # 单次 tick 隔了这么久 = 进程被挂起过（笔记本休眠）


def _beat() -> None:
    """记录一次心跳。有新页面连上（含刷新）就取消待退出状态。"""
    with _LIFE_LOCK:
        _LIFE["last_beat"] = time.time()
        _LIFE["seen"] = True
        _LIFE["beats"] = int(_LIFE.get("beats") or 0) + 1
        _LIFE["close_at"] = None


def _mark_closing() -> None:
    with _LIFE_LOCK:
        _LIFE["close_at"] = time.time()


@web.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    """页面每隔几秒打一次，证明「还有人看着」。"""
    _beat()
    return jsonify({"ok": True})


@web.route("/api/closing", methods=["POST"])
def api_closing():
    """页面被关掉/跳走时用 sendBeacon 上报（body 不解析）。"""
    _mark_closing()
    return "", 204


@web.route("/api/alive")
def api_alive():
    """身份探针：单实例守卫用它确认「那个端口上跑的确实是本程序」。

    光比 pid 不够 —— 进程号会被系统复用，必须让端口自己报出身份。
    - `beats` 是诊断字段：长期为 0 说明前端心跳没接上，程序会一直不退出
    - `closing` 表示该实例已收到「页面关闭」上报、马上要自己退了，
      此时不能再把用户往它上面引（否则会打开一个随即失效的页面）
    """
    with _LIFE_LOCK:
        beats = int(_LIFE.get("beats") or 0)
        closing = _LIFE.get("close_at") is not None
        mode = str(_LIFE.get("mode") or "browser")
    try:
        from . import win_window
        boot = win_window.boot_info()
    except Exception:                       # noqa: BLE001
        boot = None
    return jsonify({"ok": True, "app": APP_ID, "pid": os.getpid(),
                    "beats": beats, "closing": closing, "mode": mode,
                    "boot": boot})


@web.route("/api/focus", methods=["GET", "POST"])
def api_focus():
    """把本实例的窗口拉回前台（用户重复双击时，新进程走这条把它叫出来）。

    没有窗口（浏览器模式）时返回 ok=False，调用方自行退回「开浏览器」。
    """
    from . import win_window
    return jsonify({"ok": bool(win_window.focus_window())})


@web.route("/api/window/minimize", methods=["POST"])
def api_window_minimize():
    from . import win_window
    return jsonify({"ok": bool(win_window.minimize_window())})


@web.route("/api/window/maximize", methods=["GET", "POST"])
def api_window_maximize():
    """GET 查询窗口是否最大化，POST 在最大化 / 还原之间切换。

    GET 那路是给前端初始化用的：页面刷新后按钮图标要能跟窗口的真实状态对上。
    """
    from . import win_window
    if request.method == "GET":
        return jsonify({"ok": True, "maximized": win_window.is_maximized()})
    state = win_window.toggle_maximize_window()
    return jsonify({"ok": state is not None, "maximized": state})


@web.route("/api/window/close", methods=["POST"])
def api_window_close():
    """关闭应用窗口 = 退出程序。

    先回响应、**稍后**再销毁：销毁窗口会让主线程走完 `webview.start()` 并
    调用 `os._exit(0)`，同步销毁的话这个响应会半路断掉（前端白报一次失败）。
    """
    from . import win_window
    threading.Timer(0.2, win_window.close_window).start()
    return jsonify({"ok": True})


@web.route("/api/window/boot", methods=["POST"])
def api_window_boot():
    """前端的启动握手：「页面真的画出来了」。

    窗口是「先出现、后画内容」的，中间那几秒窗口只铺了一层和页面底色相同的
    background_color，看上去就是没渲染。前端首帧完成后打这个信号，后端拿它
    算真实耗时写日志，并在迟迟收不到信号时主动重载页面（见 win_window）。
    """
    from . import win_window
    data = request.get_json(silent=True) or {}
    win_window.note_boot(data)
    return jsonify({"ok": True})


def _has_running_task() -> bool:
    with _TASKS_LOCK:
        return any(t["status"] in ("pending", "running") for t in _TASKS.values())


def _stop_all_tasks() -> None:
    with _TASKS_LOCK:
        for t in _TASKS.values():
            if t["status"] in ("pending", "running"):
                t["stop_flag"] = True


def _shutdown(reason: str) -> None:
    """退出进程：先给正在跑的任务留点收尾时间，避免留下半个文件。"""
    deadline = time.time() + DRAIN_TIMEOUT
    while time.time() < deadline and _has_running_task():
        time.sleep(0.3)

    if _has_running_task():
        console.log_event(f"{reason}：仍有任务未结束，已请求停止")
        _stop_all_tasks()
        deadline = time.time() + STOP_TIMEOUT
        while time.time() < deadline and _has_running_task():
            time.sleep(0.3)

    console.log_event(f"程序退出（{reason}）")
    _clear_instance()

    # os._exit 而不是 sys.exit：
    #   - 看门狗是 daemon 线程，正常返回推不动 app.run() 的主循环
    #   - 不跑 atexit、不清理 Flask，但这些在这里都不需要（没有待 flush 的缓冲）
    #   - PyInstaller onefile 的引导进程在等子进程退出，子进程一退它跟着退，
    #     所以不需要额外去 kill 父进程
    os._exit(0)


def _watchdog() -> None:
    prev = time.time()
    while True:
        time.sleep(WATCHDOG_TICK)
        now = time.time()
        gap = now - prev
        prev = now

        with _LIFE_LOCK:
            if _LIFE["exiting"]:
                return
            close_at = _LIFE["close_at"]
            last = _LIFE["last_beat"]
            seen = _LIFE["seen"]
            started = _LIFE["started"]

        # 单次 tick 间隔远大于 1 秒 = 这中间整个进程被挂起过（合上笔记本盖）。
        # 醒来那一瞬间不能让「心跳断了很久」成立，否则一合盖再打开，程序就
        # 被自己杀掉了。把心跳基准推到当前即可 —— 浏览器醒来会立刻重新心跳。
        if gap > SUSPEND_GAP and last is not None:
            with _LIFE_LOCK:
                _LIFE["last_beat"] = now
            last = now

        reason = None
        if close_at is not None and now - close_at >= CLOSE_GRACE:
            reason = "页面已关闭"
        elif seen and last is not None and now - last >= IDLE_TIMEOUT:
            reason = "页面连接已断开，浏览器可能已崩溃"
        elif not seen and now - started >= FIRST_SEEN_TIMEOUT:
            reason = "启动后一直没有打开页面"

        if reason:
            with _LIFE_LOCK:
                if _LIFE["exiting"]:
                    return
                _LIFE["exiting"] = True
            _shutdown(reason)
            return


# ---------------------------------------------------------------------------
# 单实例守卫：重复双击不再堆积进程
# ---------------------------------------------------------------------------
def _instance_file() -> Path:
    return cfg_mod.get_data_dir() / "running.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000，权限最小、不需要管理员
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _existing_instance() -> Optional[int]:
    """已有实例在跑、且它还没准备退出，就返回它的端口，否则 None。"""
    try:
        info = json.loads(_instance_file().read_text(encoding="utf-8"))
        pid = int(info.get("pid") or 0)
        port = int(info.get("port") or 0)
    except (OSError, ValueError, TypeError):
        return None
    if not port or not _pid_alive(pid):
        return None

    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/alive", timeout=2
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if data.get("app") != APP_ID or int(data.get("pid") or 0) != pid:
        return None
    # 对方已经在退出的路上（用户刚关掉标签页、还在宽限期里），
    # 这时候把用户引过去只会打开一个马上失效的页面 —— 当作没有实例，
    # 本次进程接管。旧实例随后退出时会发现 running.json 已经不是自己的 pid，
    # 于是不会误删我们的记录。
    if data.get("closing"):
        return None
    return port


def _write_instance(port: int) -> None:
    try:
        _instance_file().write_text(
            json.dumps({"pid": os.getpid(), "port": port,
                        "started": int(time.time())}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _clear_instance() -> None:
    """只删自己写的那份记录，别抹掉别人的。"""
    try:
        f = _instance_file()
        info = json.loads(f.read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) == os.getpid():
            f.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


# ---------------------------------------------------------------------------
# 进程级单实例锁（命名 Mutex）
# ---------------------------------------------------------------------------
# 光靠 running.json 拦不住「关掉窗口 → 立刻再双击」：那一刻前一个进程还在
# 退出的路上，running.json 已经被它清掉，守卫就认为「没有实例」放行，于是新
# 进程接管。两个进程的 WebView2 去抢同一个 user data folder，首帧从 4 秒被拖
# 到几十秒 —— 用户看到的就是「打开了但什么都没渲染」。
#
# 命名 Mutex 是内核对象：进程一退（哪怕崩溃）系统立刻释放。拿得到 = 真的没
# 别人在跑，这才是可靠的互斥。用 Local\ 而不是 Global\ —— 单用户桌面应用，
# 没必要跨会话，也省掉权限问题。
_SINGLE_MUTEX = None
_MUTEX_NAME = "Local\\douyin-tool-single-instance-v1"


def _try_lock_single_instance() -> bool:
    """尝试成为唯一实例。True = 拿到锁（或系统不支持，此时不拦人）。"""
    global _SINGLE_MUTEX
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            return True                     # 创建失败就别挡用户
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(h)
            return False
        _SINGLE_MUTEX = h                   # 保活：进程退出时由系统释放
        return True
    except Exception:                       # noqa: BLE001
        return True


def _auto_exit_enabled(open_browser: bool) -> bool:
    """是否启用「关掉页面就退出 + 单实例」。

    默认以 `open_browser` 为准：自动开浏览器 = 双击 exe 的正常路径，启用；
    `--no-browser`（脚本调用、调试）不启用，免得服务莫名消失。
    环境变量 `DYD_AUTO_EXIT=1/0` 可强行覆盖。    """
    env = os.environ.get("DYD_AUTO_EXIT", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    return open_browser


# ---------------------------------------------------------------------------
# 安全：只允许抖音域名
# ---------------------------------------------------------------------------
_ALLOWED_HOSTS = {
    "www.douyin.com", "douyin.com", "v.douyin.com",
    "www.iesdouyin.com", "iesdouyin.com", "live.douyin.com",
    "m.douyin.com",
}
# 抖音各子域（如 jingxuan 精选、discover 发现等）都走同一套 CDN，
# 直接按后缀放行，避免以后又冒出新子域导致「只允许抖音域名」误拦。
_ALLOWED_HOST_SUFFIXES = (".douyin.com", ".iesdouyin.com")


def _is_allowed_host(host: str) -> bool:
    if not host:
        return False
    if host in _ALLOWED_HOSTS:
        return True
    return host.endswith(_ALLOWED_HOST_SUFFIXES)


def _check_url(raw: str) -> Optional[str]:
    """校验链接是否属于允许的域名。返回错误信息或 None。"""
    text = (raw or "").strip()
    if not text:
        return "链接为空。"

    # 纯数字 aweme_id 或 sec_user_id 直接放行
    if text.isdigit() or core.extract_sec_user_id(text) == text:
        return None

    # 从可能含杂质的文本里找 URL
    import re
    m = re.search(r"https?://[^\s\u4e00-\u9fff]+", text)
    if not m:
        return "没能在输入里找到链接，请粘贴完整的分享链接或 cURL。"
    url = m.group(0)

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "链接格式不正确。"

    if not _is_allowed_host(host):
        return f"出于安全考虑，只允许抖音域名，当前为：{host}"
    return None


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@web.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# 配置接口
# ---------------------------------------------------------------------------
@web.route("/api/config", methods=["GET"])
def get_config():
    cfg = cfg_mod.load_config()
    cookie = cfg.get("cookie") or ""
    # Cookie 打码回显，避免泄露
    masked = ""
    if cookie:
        masked = "已设置"

    return jsonify({
        "download_dir": str(cfg_mod.resolve_download_dir(cfg)),
        "proxy": cfg.get("proxy", ""),
        "naming": cfg.get("naming", ""),
        "default_max_counts": cfg.get("default_max_counts", 20),
        "cookie_set": bool(cookie),
        "cookie_masked": masked,
        "cookie_updated_at": cfg.get("cookie_updated_at", 0),
        "browser": (ck.detect_browser() or ("", ""))[0],
    })


@web.route("/api/config", methods=["POST"])
def set_config():
    data = request.get_json(silent=True) or {}
    updates: Dict[str, Any] = {}

    try:
        if "download_dir" in data:
            if not isinstance(data["download_dir"], str) or "\0" in data["download_dir"]:
                raise ValueError("下载目录必须是有效的文本路径。")
            updates["download_dir"] = data["download_dir"].strip() or "Download"
        if "proxy" in data:
            updates["proxy"] = validation.proxy_url(data["proxy"])
        if "naming" in data:
            updates["naming"] = validation.naming_template(data["naming"])
        if "default_max_counts" in data:
            updates["default_max_counts"] = validation.nonnegative_int(data["default_max_counts"])
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    # Cookie 统一由「自动登录」接口管理，这里只支持显式清除
    if data.get("clear_cookie"):
        updates["cookie"] = ""
        updates["cookie_updated_at"] = 0

    cfg_mod.update_config(**updates)
    return jsonify({"ok": True, "message": "配置已保存。"})


# ---------------------------------------------------------------------------
# 自动登录（CDP）
# ---------------------------------------------------------------------------
_LOGIN_STATE: Dict[str, Any] = {
    "status": "idle",        # idle|starting|waiting_login|reading|done|failed|cancelled
    "message": "",
    "logged_in": False,
    "browser": "",
    "updated": 0.0,
}
_LOGIN_LOCK = threading.RLock()
_LOGIN_CANCEL = threading.Event()
_LOGIN_BUSY = ("starting", "waiting_login", "reading")


def _login_get() -> Dict[str, Any]:
    with _LOGIN_LOCK:
        return dict(_LOGIN_STATE)


def _login_set(**kw: Any) -> None:
    with _LOGIN_LOCK:
        _LOGIN_STATE.update(kw)
        _LOGIN_STATE["updated"] = time.time()


@web.route("/api/login/status")
def login_status():
    st = _login_get()
    st["ok"] = True
    st["cookie_set"] = bool(cfg_mod.load_config().get("cookie"))
    return jsonify(st)


@web.route("/api/login/start", methods=["POST"])
def login_start():
    """启动自动登录。

    body: ``{"interactive": true|false}``
      - true  —— 打开可见窗口，等待用户扫码登录（最长 180 秒）
      - false —— 只快速同步已有登录态（窗口移到屏幕外）
    """
    data = request.get_json(silent=True) or {}
    interactive = data.get("interactive", False)
    if not isinstance(interactive, bool):
        return jsonify({"ok": False, "message": "登录选项必须是布尔值。"}), 400

    found = ck.detect_browser()
    with _LOGIN_LOCK:
        if _LOGIN_STATE["status"] in _LOGIN_BUSY:
            return jsonify({"ok": False, "message": "登录流程正在进行中，请稍候。"}), 409
        if not found:
            _login_set(status="failed", browser="",
                       message="未找到 Chrome 或 Edge，请先安装任一款 Chromium 内核浏览器。")
            return jsonify({"ok": False, "message": "未找到可用浏览器。"}), 400
        _LOGIN_CANCEL.clear()
        _login_set(status="starting", browser=found[0],
                   message="正在启动浏览器…", logged_in=False)

    def worker() -> None:
        def on_log(msg: str) -> None:
            _login_set(status="waiting_login" if interactive else "reading", message=msg)

        def should_stop() -> bool:
            return _LOGIN_CANCEL.is_set()

        _login_set(status="waiting_login" if interactive else "reading")
        try:
            cookie, msg = bl.fetch_cookie(
                interactive=interactive, on_log=on_log, should_stop=should_stop,
            )
        except Exception as e:  # noqa: BLE001
            cookie, msg = None, f"登录失败：{type(e).__name__}: {e}"

        with _LOGIN_LOCK:
            if _LOGIN_CANCEL.is_set():
                _login_set(status="cancelled", logged_in=False, message="已取消。")
            elif cookie and bl.is_logged_in(cookie):
                try:
                    cfg_mod.update_config(cookie=cookie, cookie_updated_at=int(time.time()))
                except OSError:
                    _login_set(status="failed", logged_in=False,
                               message="登录态保存失败，请检查目录权限和磁盘空间后重试。")
                else:
                    _login_set(status="done", logged_in=True,
                               message=msg or "登录成功，可以开始下载了。")
            else:
                _login_set(status="failed", logged_in=False,
                           message=msg or "未能获取登录态。")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "status": "starting", "browser": found[0]}), 202


@web.route("/api/login/cancel", methods=["POST"])
def login_cancel():
    with _LOGIN_LOCK:
        if _LOGIN_STATE["status"] in _LOGIN_BUSY:
            _LOGIN_CANCEL.set()
            _login_set(message="正在取消…")
    return jsonify({"ok": True})


@web.route("/api/login/forget", methods=["POST"])
def login_forget():
    """退出登录：清空 cookie 并删除独立 profile 目录。"""
    if _login_get()["status"] in _LOGIN_BUSY:
        return jsonify({"ok": False, "message": "登录流程进行中，请先等待结束。"}), 409
    cfg_mod.update_config(cookie="", cookie_updated_at=0)
    try:
        import shutil
        shutil.rmtree(bl.profile_dir(), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    _login_set(status="idle", logged_in=False, message="已退出登录，下次需重新登录。")
    return jsonify({"ok": True, "message": "已清除登录态。"})


# ---------------------------------------------------------------------------
# 解析预览
# ---------------------------------------------------------------------------
@web.route("/api/parse", methods=["POST"])
def parse_link():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("url", "")).strip()

    err = _check_url(raw)
    if err:
        return jsonify({"ok": False, "message": err}), 400

    try:
        info = core.run_async(core.parse_link_info(raw))
        return jsonify({"ok": True, "info": info})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)}), 400


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
@web.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("url", "")).strip()
    mode = data.get("mode", "one")
    # 强制重新下载：忽略下载记录，直接重下（对应 CLI 的 --no-skip）
    force = data.get("force", False)
    try:
        if mode not in ("one", "post"):
            raise ValueError("下载模式必须为 one 或 post。")
        if not isinstance(force, bool):
            raise ValueError("强制下载选项必须是布尔值。")
        max_counts = validation.nonnegative_int(data.get("max_counts"), default=None)
        date_start, date_end = validation.date_range(data.get("date_start"), data.get("date_end"))
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    err = _check_url(raw)
    if err:
        return jsonify({"ok": False, "message": err}), 400

    cfg = cfg_mod.load_config()
    if not cfg.get("cookie"):
        # 实测：未登录时 detail 接口必定 403，直接引导用户登录更省事
        return jsonify({
            "ok": False,
            "need_login": True,
            "message": "尚未登录抖音。请先点上方「打开登录窗口」完成一次性登录。",
        }), 400

    # 任务标题用规范化后的地址（抹掉 ?modal_id=xxx&type=general 这类无关参数）
    target = core.normalize_douyin_url(raw) or raw

    if mode == "post":
        mc = max_counts
        try:
            tid = _new_task("post", target, total_hint=mc or 0)
        except TaskQueueFull as exc:
            return jsonify({"ok": False, "message": str(exc)}), 429
        _run_in_thread(tid, lambda cb: core.download_user_posts(
            raw, cfg, on_progress=cb, max_counts=mc,
            date_start=date_start, date_end=date_end,
            skip_downloaded=not force,
            should_stop=_should_stop(tid),
        ))
    else:
        try:
            tid = _new_task("one", target)
        except TaskQueueFull as exc:
            return jsonify({"ok": False, "message": str(exc)}), 429
        _run_in_thread(tid, lambda cb: core.download_one(
            raw, cfg, on_progress=cb, skip_downloaded=not force,
            should_stop=_should_stop(tid),
        ))

    return jsonify({"ok": True, "task_id": tid})


@web.route("/api/progress/<tid>")
def progress(tid: str):
    t = _get(tid)
    if not t:
        return jsonify({"ok": False, "message": "任务不存在"}), 404
    t.pop("stop_flag", None)
    return jsonify({"ok": True, "task": t})


@web.route("/api/tasks")
def list_tasks():
    with _TASKS_LOCK:
        items = [dict(v) for v in _TASKS.values()]
    for it in items:
        it.pop("stop_flag", None)
    items.sort(key=lambda x: x["created"], reverse=True)
    return jsonify({"ok": True, "tasks": items[:50]})


@web.route("/api/cancel/<tid>", methods=["POST"])
def cancel_task(tid: str):
    with _TASKS_LOCK:
        task = _TASKS.get(tid)
        if task is None:
            return jsonify({"ok": False, "message": "任务不存在。"}), 404
        if task["status"] not in ("pending", "running"):
            return jsonify({"ok": False, "message": "任务已结束。"}), 409
        task.update(stop_flag=True, message="正在取消...")
    return jsonify({"ok": True})


@web.route("/api/open-folder", methods=["POST"])
def open_folder():
    """在资源管理器中打开下载目录。"""
    cfg = cfg_mod.load_config()
    folder = cfg_mod.resolve_download_dir(cfg)
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return jsonify({"ok": True, "folder": str(folder)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)}), 500


@web.route("/api/reset-history", methods=["POST"])
def reset_history():
    cfg_mod.reset_history()
    return jsonify({"ok": True, "message": "下载历史已清空。"})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    """创建并配置 Flask 应用，供服务入口和测试复用。"""
    application = Flask(
        __name__,
        template_folder=str(cfg_mod.get_resource_dir() / "web" / "templates"),
        static_folder=str(cfg_mod.get_resource_dir() / "web" / "static"),
    )
    application.register_blueprint(web)
    return application


app = create_app()


def run_server(host: str = "127.0.0.1", port: int = 0,
               open_browser: bool = True) -> None:
    """启动 Web 服务（阻塞）。port=0 时自动选空闲端口。

    两种承载方式：
      - **窗口模式**（默认）：自己开一个无边框窗口装载页面，没有浏览器外壳，
        关掉窗口即退出进程。`DYD_WINDOW=0` 可强行退回浏览器模式（排查用）。
      - **浏览器模式**：用系统浏览器打开，靠看门狗判断页面是否还在。
    """
    import socket
    import webbrowser

    from . import win_window

    auto_exit = _auto_exit_enabled(open_browser)

    env_window = os.environ.get("DYD_WINDOW", "").strip().lower()
    use_window = (open_browser
                  and env_window not in ("0", "false", "no", "off")
                  and win_window.is_supported())
    mode = "window" if use_window else "browser"

    # ---- 单实例：已经有一份在跑，就把它叫到前台，本进程直接退出 ----
    # 这是「进程越堆越多」的根因 —— 界面关掉后服务不会自己退，
    # 再双击一次就多一份。只在「自动打开界面」的模式下拦截，
    # 脚本用 --no-browser 调起时不拦，免得服务被莫名吞掉。
    if auto_exit and open_browser:
        old_port = _existing_instance()
        # running.json 查不到 ≠ 没有实例：对方可能刚起来还没来得及写，或者正在
        # 退出、已经把文件清掉了。Mutex 才是准的 —— 拿不到锁就说明确实有人在跑。
        if old_port is None and not _try_lock_single_instance():
            console.log_event("另一个实例仍占着启动锁（正在启动或正在退出），先等它一下")
            for _ in range(24):                     # 最多等 12 秒
                time.sleep(0.5)
                old_port = _existing_instance()
                if old_port or _try_lock_single_instance():
                    break
            else:
                # 等不到锁、也找不到能用的实例：宁可多开一次，也不能让用户打不开
                console.log_event("等了 12 秒仍拿不到启动锁，照常启动")
        if old_port:
            old_url = f"http://{host}:{old_port}"
            console.safe_print(f"\n  已经有一个实例在运行：{old_url}")
            console.safe_print("  已为你打开它的界面，本次不再重复启动。\n")
            console.log_event(f"检测到已有实例（端口 {old_port}），本次未启动")
            focused = False
            if mode == "window":
                try:
                    import urllib.request
                    with urllib.request.urlopen(
                        f"{old_url}/api/focus", timeout=2
                    ) as r:
                        focused = bool(
                            json.loads(r.read().decode("utf-8")).get("ok"))
                except Exception:  # noqa: BLE001
                    focused = False
            if not focused:
                try:
                    webbrowser.open(old_url)
                except Exception:  # noqa: BLE001
                    pass
            return

    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            port = s.getsockname()[1]

    url = f"http://{host}:{port}"

    with _LIFE_LOCK:
        _LIFE["mode"] = mode

    if auto_exit:
        _write_instance(port)

    if use_window:
        # pywebview 要求 GUI 跑在主线程，所以 Flask 挪进守护线程。
        threading.Thread(
            target=lambda: app.run(host=host, port=port, debug=False,
                                   threaded=True, use_reloader=False),
            daemon=True,
        ).start()
        console.safe_print(f"\n  {APP_TITLE} 已启动")
        console.safe_print(
            f"  下载目录：{cfg_mod.resolve_download_dir(cfg_mod.load_config())}")
        console.safe_print("  关闭窗口即退出程序\n")
        if win_window.run_window(url, APP_TITLE):
            # 用户关掉了窗口 —— 这是主动退出，不必等看门狗再判一次
            _shutdown("窗口已关闭")
            return
        # 窗口起不来（缺 WebView2 / 组件异常）→ 退回浏览器，别把用户卡在没界面
        console.log_event("窗口创建失败，退回系统浏览器")
        console.safe_print("  未能创建应用窗口，已改用系统浏览器打开\n")
        with _LIFE_LOCK:
            _LIFE["mode"] = "browser"

    if auto_exit:
        threading.Thread(target=_watchdog, daemon=True).start()
        console.log_event(f"启动：{url}（pid={os.getpid()}，关掉页面即退出）")

    if open_browser:
        def _open() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    console.safe_print(f"\n  {APP_TITLE} 已启动")
    console.safe_print(f"  请在浏览器访问：{url}")
    console.safe_print(
        f"  下载目录：{cfg_mod.resolve_download_dir(cfg_mod.load_config())}"
    )
    if auto_exit:
        console.safe_print("  关闭浏览器页面后程序会自动退出")
    else:
        console.safe_print("  按 Ctrl+C 退出")
    console.safe_print("")

    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
