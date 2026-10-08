# -*- coding: utf-8 -*-
"""浏览器自动登录模块（CDP 方案）。

原理
----
不破解任何加密。程序启动一个**独立 profile** 的 Chromium 内核浏览器
（与用户日常浏览器完全隔离），开启本地调试端口，通过 Chrome DevTools
Protocol 让浏览器自己把 cookie 吐出来 —— 本质是「用户授权下读自己的登录态」。

流程
----
1. 启动 chrome/edge：``--remote-debugging-port=<port>`` + ``--user-data-dir=<独立目录>``
2. HTTP 轮询 ``/json/version`` 拿 ``webSocketDebuggerUrl``
3. CDP 顺序（不可换）：
   ``Target.createTarget`` → ``Target.attachToTarget(flatten=True)``
   → ``Network.enable`` → ``Page.enable`` → ``Page.navigate(douyin.com)``
4. ``interactive=True``：每 3 秒轮询 cookie，直到出现 sessionid 或超时 180 秒
   ``interactive=False``：等待数秒后直接读（稳态复用登录态）
5. ``Browser.close`` 优雅关闭 —— **必须**，强杀会导致部分 cookie 不落盘

实测结论（2026-09，Chrome 154 / Edge 154）
------------------------------------------
- 有头 + 独立 profile + 远程调试：稳定读到 25–29 条明文 cookie
  （含 ttwid / UIFID / __ac_nonce / __ac_signature）
- 登录态（sessionid）会持久化到 profile 目录，二次启动复用即可读回
- 只有带 ``expires`` 的持久 cookie 才会落盘（网站通过响应头设的都带）
- Edge 同样可用，作为 Chrome 的兜底
- websockets 12.0 只有 legacy API，用 ``websockets.connect``
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as cfg_mod
from . import console
from .cookie_reader import (
    detect_browser,
    free_port,
    parse_cookie_string,
    wait_cdp,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DOUYIN_HOME = "https://www.douyin.com/"
DOUYIN_URLS = [DOUYIN_HOME, "https://www.douyin.com/discover"]

LOGIN_COOKIE_KEYS = ("sessionid", "sessionid_ss")

NAV_WAIT_STEADY = 6.0            # 稳态读取：导航后等待
LOGIN_POLL_INTERVAL = 3.0        # 登录轮询间隔
LOGIN_TIMEOUT = 180.0            # 首次登录最长等待
CDP_READY_TIMEOUT = 25.0         # 调试通道就绪超时
CLOSE_WAIT = 15.0                # 优雅关闭等待

PROFILE_DIRNAME = "browser_profile"
PORT_FILENAME = "cdp_port.txt"

# 稳态（非交互）读取时把窗口挪出屏幕，避免一闪而过打扰用户
OFFSCREEN_FLAGS = ["--window-position=-2500,-2500", "--window-size=900,700"]


# ---------------------------------------------------------------------------
# 路径与进程工具
# ---------------------------------------------------------------------------
def profile_dir() -> Path:
    """独立浏览器 profile 目录（登录态就存在这里）。

    放在**数据目录**下：冻结态 = exe 同级/browser_profile，
    开发态 = <项目根>/data/browser_profile。
    """
    p = cfg_mod.get_data_dir() / PROFILE_DIRNAME
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _port_file(prof: Path) -> Path:
    return prof / PORT_FILENAME


def _read_saved_port(prof: Path) -> Optional[int]:
    try:
        txt = _port_file(prof).read_text(encoding="utf-8").strip()
        return int(txt) if txt else None
    except (OSError, ValueError):
        return None


def _write_saved_port(prof: Path, port: int) -> None:
    try:
        _port_file(prof).write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def _decode(raw: bytes) -> str:
    """Windows 命令输出多为 GBK，逐个编码尝试避免崩溃。"""
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return ""


def _enum_browser_cmdlines() -> str:
    """枚举 chrome/msedge 进程的「命令行」，返回原始文本（拿不到则空串）。

    说明：早期用 `wmic`，但 **Windows 11 24H2 已移除 wmic**，
    故增加 PowerShell CIM 兜底（实测可用；wmic 存在时优先，更快）。
    """
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        out = subprocess.run(
            ["wmic", "process", "where",
             "name='chrome.exe' or name='msedge.exe'",
             "get", "processid,commandline", "/format:csv"],
            capture_output=True, timeout=20, creationflags=no_win,
        )
        text = _decode(out.stdout or b"")
        if text.strip():
            return text
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        cmd = ("Get-CimInstance Win32_Process -Filter "
               "\"Name='chrome.exe' or Name='msedge.exe'\" | "
               "Select-Object ProcessId,CommandLine | "
               "ConvertTo-Csv -NoTypeInformation")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, timeout=60, creationflags=no_win,
        )
        return _decode(out.stdout or b"")
    except (OSError, subprocess.SubprocessError):
        return ""


def _kill_profile_processes(prof: Path) -> int:
    """杀掉占用本 profile 的残留浏览器进程（只匹配我们自己的 profile 目录名）。

    返回杀掉的进程数。匹配策略保守：命令行里必须出现 ``browser_profile``
    这个目录名，因此绝不会误伤用户日常浏览器的默认数据目录（那是 ``User Data``）。
    """
    marker = os.path.basename(str(prof)).lower()
    if not marker:
        return 0

    text = _enum_browser_cmdlines()
    if not text:
        return 0

    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    killed = 0
    for line in text.splitlines():
        low = line.lower()
        if marker not in low:
            continue
        if "chrome.exe" not in low and "msedge.exe" not in low:
            continue

        # PowerShell ConvertTo-Csv：首字段是 ProcessId
        # wmic /format:csv：末字段是 ProcessId
        pid = None
        m = re.match(r'\s*"?(\d+)"?\s*,', line)
        if m:
            pid = m.group(1)
        else:
            m2 = re.search(r",\s*\"?(\d+)\"?\s*$", line)
            if m2:
                pid = m2.group(1)
        if not pid:
            continue

        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", pid],
                capture_output=True, timeout=15, creationflags=no_win,
            )
            killed += 1
        except (OSError, subprocess.SubprocessError):
            continue
    return killed


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    """三级清理：先等优雅退出，再 taskkill /T（Chrome 有子进程树）。"""
    if proc is None:
        return
    try:
        proc.wait(timeout=CLOSE_WAIT)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# cookie 处理
# ---------------------------------------------------------------------------
def _has_session(cookies: List[Dict[str, Any]]) -> bool:
    names = {c.get("name") for c in cookies}
    return any(k in names for k in LOGIN_COOKIE_KEYS)


def is_logged_in(cookie_str: str) -> bool:
    """判断 cookie 串里是否含登录凭据（sessionid）。"""
    if not cookie_str:
        return False
    d = parse_cookie_string(cookie_str)
    return any(k in d for k in LOGIN_COOKIE_KEYS)


def cookies_to_string(cookies: List[Dict[str, Any]]) -> str:
    """把 CDP 的 cookie 列表拼成 ``k=v; k2=v2``。

    只保留 douyin 域；同名 cookie 优先取 domain 更长（更具体）的那条。
    """
    dy = [c for c in cookies if "douyin" in (c.get("domain") or "")]
    best: Dict[str, Dict[str, Any]] = {}
    for c in sorted(dy, key=lambda x: len(x.get("domain") or ""), reverse=True):
        name, value = c.get("name"), c.get("value")
        if name and value is not None and name not in best:
            best[name] = c
    return "; ".join(f"{n}={c['value']}" for n, c in best.items())


# ---------------------------------------------------------------------------
# CDP 异步流程
# ---------------------------------------------------------------------------
async def _cdp_flow(
    ws_url: str,
    *,
    interactive: bool,
    on_log: Callable[[str], None],
    should_stop: Optional[Callable[[], bool]],
) -> Tuple[List[Dict[str, Any]], bool, str]:
    """CDP 全流程。返回 (cookies, 是否登录, 说明)。"""
    import websockets  # 延迟导入：未安装时给友好报错

    cookies: List[Dict[str, Any]] = []
    logged = False
    message = ""

    ws = await websockets.connect(ws_url, max_size=128 * 1024 * 1024, open_timeout=20)
    counter = itertools.count(1)

    async def call(method: str, params: Optional[Dict] = None,
                   sid: Optional[str] = None, timeout: float = 30.0) -> Dict:
        mid = next(counter)
        payload: Dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if sid:
            payload["sessionId"] = sid
        await ws.send(json.dumps(payload))

        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError(f"CDP {method} 超时")
            raw = await asyncio.wait_for(ws.recv(), timeout=remain)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(f"CDP {method} 失败: {err.get('message') or err}")
                return msg.get("result") or {}
            # 其余是事件通知，直接忽略

    try:
        r = await call("Target.createTarget", {"url": "about:blank"})
        target_id = r.get("targetId")
        if not target_id:
            return [], False, "无法创建浏览器标签页。"

        r = await call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = r.get("sessionId")
        if not sid:
            return [], False, "无法连接浏览器调试会话。"

        # ★ 必须先 enable，否则 getCookies 返回空
        await call("Network.enable", {}, sid)
        await call("Page.enable", {}, sid)

        on_log("正在打开抖音…")
        await call("Page.navigate", {"url": DOUYIN_HOME}, sid)

        async def read() -> List[Dict[str, Any]]:
            res = await call("Network.getCookies", {"urls": DOUYIN_URLS}, sid, timeout=20)
            items = res.get("cookies") or []
            if not items:
                try:
                    res2 = await call("Storage.getCookies", {}, sid, timeout=20)
                    items = res2.get("cookies") or []
                except Exception:  # noqa: BLE001
                    pass
            return items

        if interactive:
            on_log("请在打开的浏览器窗口里登录抖音（扫码或账号密码）…")
            deadline = time.time() + LOGIN_TIMEOUT
            while True:
                if should_stop and should_stop():
                    message = "已取消"
                    break
                if time.time() >= deadline:
                    message = f"等待登录超时（{int(LOGIN_TIMEOUT)} 秒），请重试。"
                    break
                await asyncio.sleep(LOGIN_POLL_INTERVAL)
                cookies = await read()
                if _has_session(cookies):
                    logged = True
                    message = "登录成功"
                    break
        else:
            await asyncio.sleep(NAV_WAIT_STEADY)
            cookies = await read()
            if not cookies:
                # 页面可能还没渲染好（被动等待，不打断用户），再试一次
                await asyncio.sleep(3.0)
                cookies = await read()
            logged = _has_session(cookies)
            message = "登录态有效" if logged else "未检测到登录态"

    except Exception as e:  # noqa: BLE001
        return cookies, logged, f"读取失败：{type(e).__name__}: {e}"
    finally:
        # 优雅关闭，让 cookie 落盘；失败也不能影响返回
        try:
            await call("Browser.close", timeout=6)
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    return cookies, logged, message


# ---------------------------------------------------------------------------
# 同步主入口
# ---------------------------------------------------------------------------
def fetch_cookie(
    interactive: bool = True,
    browser: Optional[str] = None,
    on_log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Tuple[Optional[str], str]:
    """启动独立 profile 浏览器 → CDP 读 cookie → 优雅关闭。

    参数
    ----
    interactive : True 则等待用户登录（最长 180 秒）；False 只快速读已有登录态。
    browser     : 指定 "chrome" / "edge"；None 则自动探测（Chrome 优先）。
    on_log      : 进度回调（用于网页端实时显示）。
    should_stop : 返回 True 时中断登录等待。

    返回
    ----
    ``(cookie字符串 或 None, 说明文字)``
    """
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:  # noqa: BLE001
                pass
        else:
            console.safe_print(f"  {msg}")

    found = detect_browser(browser)
    if not found:
        return None, (
            "未找到 Chrome 或 Edge。本功能依赖 Chromium 内核浏览器，"
            "请先安装 Google Chrome 或 Microsoft Edge。"
        )
    name, exe = found
    prof = profile_dir()

    def launch() -> Tuple[int, Optional[subprocess.Popen]]:
        """启动一个带调试端口的独立实例。"""
        p = free_port()
        _write_saved_port(prof, p)
        cmd = [
            exe,
            f"--remote-debugging-port={p}",
            f"--user-data-dir={prof}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-features=Translate,InfiniteSessionRestore",
        ]
        if not interactive:
            cmd += OFFSCREEN_FLAGS
        cmd.append("about:blank")
        log(f"正在启动 {name}（调试端口 {p}）…")
        child = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p, child

    # ---- 复用：上次的实例若还活着，直接接管 ----
    proc: Optional[subprocess.Popen] = None
    port: Optional[int] = None
    saved = _read_saved_port(prof)
    if saved and wait_cdp(saved, timeout=2):
        port = saved
        log(f"复用已运行的 {name} 实例（端口 {port}）")

    # ---- 启动；失败才做重量级的残留进程清理并重试一次 ----
    if port is None:
        try:
            port, proc = launch()
        except OSError as e:
            return None, f"启动 {name} 失败：{e}"

        ws_url = wait_cdp(port, timeout=CDP_READY_TIMEOUT)
        if not ws_url:
            log("调试通道未就绪，正在清理残留浏览器进程后重试…")
            _terminate(proc)
            killed = _kill_profile_processes(prof)
            if killed:
                log(f"已清理 {killed} 个残留进程")
            try:
                port, proc = launch()
            except OSError as e:
                return None, f"启动 {name} 失败：{e}"
    else:
        ws_url = wait_cdp(port, timeout=CDP_READY_TIMEOUT)

    if not ws_url:
        _terminate(proc)
        return None, (
            "浏览器调试通道启动超时。可能原因：被安全软件拦截、"
            "浏览器版本不支持远程调试，或上一条登录窗口未关闭。"
            "请关闭所有本程序打开过的浏览器窗口后重试。"
        )

    log("调试通道已就绪，正在读取登录态…")
    try:
        cookies, logged, msg = asyncio.run(_cdp_flow(
            ws_url,
            interactive=interactive,
            on_log=log,
            should_stop=should_stop,
        ))
    except Exception as e:  # noqa: BLE001
        _terminate(proc)
        return None, f"读取登录态失败：{type(e).__name__}: {e}"

    _terminate(proc)

    cookie_str = cookies_to_string(cookies)
    count = len(parse_cookie_string(cookie_str))

    if not logged:
        hint = (
            "未检测到登录态。请点「打开登录窗口」在弹出的浏览器里完成登录。"
            if not interactive
            else msg
        )
        return None, hint or "未完成登录。"

    if not cookie_str:
        return None, "已登录但未读取到抖音 Cookie，请重试。"

    log(f"读取完成：{count} 个字段")
    return cookie_str, f"登录成功，已获取 {count} 个 Cookie 字段。"


def read_cookie_auto(
    on_log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    browser: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """稳态自动读取（不开登录窗口，只复用已有登录态）。"""
    return fetch_cookie(interactive=False, browser=browser,
                        on_log=on_log, should_stop=should_stop)
