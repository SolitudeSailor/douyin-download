# -*- coding: utf-8 -*-
"""抖音下载核心层 —— 对 f2 的封装。

设计原则：
- **签名、令牌、接口全部交给 f2**，本模块只做编排与用户体验
- 所有对外函数都接受 progress 回调，便于 Web 层推送进度
- 异常统一包装成中文提示，让用户看得懂

⚠️ 重要：f2 在 **import 时** 就会执行 `log_setup()`，往 `./logs` 写日志文件，
   且依赖当前工作目录。因此本模块必须在导入任何 f2 模块**之前**，
   先调用 `_prepare_f2_env()` 切好目录、抢占 logger 配置。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config as cfg_mod
from . import validation

# 进度回调签名：(current: int, total: int, message: str) -> None
ProgressCB = Optional[Callable[[int, int, str], None]]

# ---------------------------------------------------------------------------
# f2 环境准备（必须在 import f2 之前调用）
# ---------------------------------------------------------------------------
_F2_READY = False


def _prepare_f2_env() -> None:
    """在导入 f2 前完成环境准备，规避 f2 的日志副作用。

    f2 的 `f2/log/logger.py` 在模块级执行 `log_setup()`：
      - 往 `./logs`（相对当前工作目录）写日志
      - 用 Singleton 保证只初始化一次

    若不动它，打包后的 exe 会因 CWD 不确定而写入失败甚至崩溃。
    对策：先把 CWD 切到**数据目录**，并预先给 f2 的 logger 挂好我们自己的 handler，
    这样 f2 的 `log_setup()` 检测到 `logger.hasHandlers()` 为真就会直接跳过。

    ⚠️ 除日志外，f2 的用户库 `AsyncUserDB("douyin_users.db")`、
    `AsyncVideoDB(...)` 用的也都是**相对 CWD 的路径**（实测 `handler.py` / `dl.py`
    里是裸文件名），所以这一次 chdir 同时决定了它们落在哪 ——
    统一收进数据目录，正是把运行时产物和源码隔开的关键。
    """
    global _F2_READY
    if _F2_READY:
        return

    data_dir = cfg_mod.get_data_dir()

    # 0) chdir 之前先把 argv[0] 锁成绝对路径。
    #    有库（实测 pywebview 的 `util.get_app_root()`）用
    #    `dirname(realpath(sys.argv[0]))` 推导「应用根目录」；chdir 之后，
    #    若 argv[0] 还是相对路径（`python -m douyin_tool`），就会被解析成
    #    `<数据目录>/src/...` 而去引用不存在的资源。这里一次性钉死。
    try:
        if sys.argv and sys.argv[0]:
            sys.argv[0] = os.path.abspath(sys.argv[0])
    except Exception:  # noqa: BLE001
        pass

    # 1) 切工作目录到数据目录，保证 f2 的 "./logs"、"./douyin_users.db" 落在可控位置
    try:
        os.chdir(data_dir)
    except OSError:
        pass

    # 2) 预先配置 f2 的 logger，阻止其 log_setup() 再往文件写
    import logging

    logger = logging.getLogger("f2")
    if not logger.hasHandlers():
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("[f2] %(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)  # 默认安静，出问题才说话
        logger.propagate = False

    _F2_READY = True


# ---------------------------------------------------------------------------
# 文件名安全化
# ---------------------------------------------------------------------------
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """把任意文本变成 Windows 合法的文件名片段。

    - 替换非法字符为下划线
    - 折叠连续空白
    - 去掉首尾的点和空格（Windows 不允许）
    - 截断超长部分
    - 规避 Windows 保留设备名
    """
    if not name:
        return "untitled"

    s = _ILLEGAL_CHARS.sub("_", str(name))
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if not s:
        return "untitled"

    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")

    if s.upper() in _WINDOWS_RESERVED:
        s = f"_{s}"

    return s or "untitled"


# ---------------------------------------------------------------------------
# f2 kwargs 组装
# ---------------------------------------------------------------------------
def _disable_f2_bark() -> None:
    """关掉 f2 内置的 Bark 通知。

    f2 的 `conf/conf.yaml` 默认 `enable_bark: true`，用户没配 key 时每次下载都会
    往 `https://api.day.app/` 发请求，刷出一串 405 ERROR 日志，干扰真正的报错。
    该开关是类属性，在这里改掉即可（必须在 DouyinHandler 实例化之前调用）。
    """
    try:
        from f2.apps.bark.utils import ClientConfManager as _BarkConf
        _BarkConf.client_conf["enable_bark"] = False
    except Exception:  # noqa: BLE001
        pass


def _cookie_value(cookie_str: str, name: str) -> str:
    """从 ``k=v; k2=v2`` 形式的 cookie 串里取某个字段的值（取不到返回空串）。"""
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part:
            continue
        key, _, value = part.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def build_kwargs(cfg: Dict[str, Any], mode: str = "one",
                 download_dir: Optional[Path] = None) -> Dict[str, Any]:
    """把我们的配置翻译成 f2 需要的 kwargs。"""
    _prepare_f2_env()
    _disable_f2_bark()

    from f2.apps.douyin.utils import ClientConfManager

    target_dir = download_dir or cfg_mod.resolve_download_dir(cfg)

    # 代理：f2 要求 {"http://": ..., "https://": ...} 形式
    proxy = validation.proxy_url(cfg.get("proxy") or "")
    proxies = {"http://": proxy or None, "https://": proxy or None}

    cookie_str = (cfg.get("cookie") or "").strip()

    # ★ 抖音边缘网关 ArgusSecurityPlugin（2026-09 起对 aweme/detail、aweme/post
    #   等接口开启）要求请求头带 `x-tt-argus`：缺它一律 403，响应体是
    #   "Blocked by ArgusSecurityPlugin Uifid Not Found"；
    #   只补 uifid 而没有 x-tt-argus 则变成 "... Signature Not Found"。
    #   网关当前不校验该头取值，固定字符串即可（实测 "1" 放行）。
    headers = {
        "User-Agent": ClientConfManager.user_agent(),
        "Referer": "https://www.douyin.com/",
        "x-tt-argus": "1",
    }
    # 与浏览器行为保持一致：cookie 里有 UIFID 时，同步发一个同名请求头
    uifid = _cookie_value(cookie_str, "UIFID")
    if uifid:
        headers["uifid"] = uifid

    return {
        "headers": headers,
        "proxies": proxies,
        "cookie": cookie_str,
        "timeout": int(cfg.get("timeout") or 10),
        "path": str(target_dir),
        "mode": mode,
        "max_retries": validation.nonnegative_int(cfg.get("max_retries"), default=5),
        # 用户选定的命名：日期_作者_文案
        "naming": validation.naming_template(cfg.get("naming") or "{create}_{nickname}_{desc}"),
        # 只下视频：关掉封面/文案/原声
        "music": False,
        "cover": False,
        "desc": False,
        # 不按作品再套一层文件夹：我们已经按作者分目录了，再套会过深
        # （f2 会用它作为开关，见 dl.py handler_download）
        "folderize": False,
    }


# ---------------------------------------------------------------------------
# 链接 → 作品 ID：本地正则优先，短链才联网
# ---------------------------------------------------------------------------
# 背景：f2 的 AwemeIdFetcher 只看「跳转后 URL 的路径」里有没有
#   video/<id> 或 note/<id>，也就是说它只认 /video/xxxx、/note/xxxx 两种形态。
#   而抖音分享出来的链接有大量形态把作品 ID 放在 **查询参数** 里，
#   最典型的就是精选/发现/个人页点开作品后的弹窗地址：
#       https://www.douyin.com/jingxuan?modal_id=7682373106140319026
#   这类链接跳转后路径仍是 /jingxuan，f2 匹配不到 → 报「未在响应的地址中找到
#   aweme_id」。所以这里先自己把 ID 抠出来，抠不到（多为 v.douyin.com 短链，
#   需要真正跟随跳转）才交给 f2 联网解析。
_AWEME_ID_PATTERNS = (
    r"/share/video/(\d{6,})",     # iesdouyin / m.douyin 分享页
    r"/share/note/(\d{6,})",      # 图文分享页
    r"/video/(\d{6,})",           # 标准作品页
    r"/note/(\d{6,})",            # 图文笔记页
    r"[?&]modal_id=(\d{6,})",     # 精选/发现/搜索/个人页的作品弹窗
    r"[?&]aweme_id=(\d{6,})",     # 部分站外跳转
    r"[?&]item_id=(\d{6,})",
    r"[?&]vid=(\d{6,})",          # 老版分享链接
)

# sec_user_id 固定以 MS4wLjAB 开头，可直接从任意文本里识别
_SEC_USER_ID_RE = re.compile(r"MS4wLjAB[A-Za-z0-9_\-]{10,}")


def extract_aweme_id(raw: str) -> Optional[str]:
    """不联网，从任意输入里抠出作品 ID；抠不到返回 None。

    支持：纯数字 ID、/video/、/note/、/share/video/、?modal_id=、
    ?aweme_id=、?item_id=、?vid=、以及含这些内容的整套分享文本。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return text
    for pattern in _AWEME_ID_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def extract_sec_user_id(raw: str) -> Optional[str]:
    """不联网，从任意输入里抠出用户 sec_user_id；抠不到返回 None。"""
    m = _SEC_USER_ID_RE.search((raw or "").strip())
    return m.group(0) if m else None


def normalize_douyin_url(raw: str) -> str:
    """把用户粘贴的链接整理成规范形式，**抹掉后面那些没用的参数**。

    例：
      https://www.douyin.com/search/%E7%94%B5...?modal_id=7613...&type=general
        → https://www.douyin.com/video/7613599571695963444
      https://www.douyin.com/user/MS4wLjAB...?modal_id=7613...
        → https://www.douyin.com/video/7613599571695963444   （作品优先，不降级成主页）

    只用于「展示」和「联网兜底时的输入」；真正的解析仍以 extract_aweme_id(raw)
    为准，所以这里就算判错也不会引入解析回归。
    """
    text = (raw or "").strip()
    if not text:
        return ""

    if text.isdigit():
        return text                                   # 纯 ID 原样保留

    # ★ 顺序铁律：作品 ID 必须先于用户 ID 判断。
    #   否则 /user/MS4wLjAB…?modal_id=7xxx（在主页点开了某条作品）会被降级成主页，
    #   与用户意图不符。
    aweme_id = extract_aweme_id(text)
    if aweme_id:
        return f"https://www.douyin.com/video/{aweme_id}"

    sec_user_id = extract_sec_user_id(text)
    if sec_user_id:
        return f"https://www.douyin.com/user/{sec_user_id}"

    # 剩下的基本只有 v.douyin.com 短链（必须联网跟随跳转），原样保留
    m = re.search(r"https?://[^\s\u4e00-\u9fff]+", text)
    return m.group(0) if m else text


# ---------------------------------------------------------------------------
# 异常包装
# ---------------------------------------------------------------------------
class DouyinError(Exception):
    """用户可读的错误。"""


class LinkParseError(DouyinError):
    """**仅**表示「链接 → ID」这一步失败，不与下游接口错误混用。

    以前没有这个类型时，_wrap_error 靠 "aweme_id" in msg 来猜，
    结果连 f2 内部的 KeyError('aweme_id') 都会被翻译成
    「无法从链接中解析出作品 ID」，严重误导排查方向。
    """


class DownloadCancelled(DouyinError):
    """用户主动取消任务 —— 这不是错误，界面该显示「已取消」而不是「失败」。

    ★ 必须继承 DouyinError：
      download_one / download_user_posts 的外层都把异常交给 _wrap_error()，
      而 _wrap_error 对 DouyinError 是**原样透传**。若不继承，取消会被
      翻译成「下载失败：...」，语义就错了，用户会以为是程序出问题。

    ★ 凡是 catch 范围覆盖到下载环节的地方，都要把它放行（re-raise）：
      - download_user_posts 里逐条下载的 except（否则取消会被塞进 failed 列表）
      - web_app._run_in_thread（且必须排在通用 except Exception 之前）
    """


def _check_stop(should_stop) -> None:
    if should_stop and should_stop():
        raise DownloadCancelled("已取消")


async def run_cancellable(coro, should_stop=None):
    """取消整个等待链，并等待其 finally 收尾，覆盖请求和正在传输的文件。"""
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            _check_stop(should_stop)
            await asyncio.wait({task}, timeout=0.1)
        _check_stop(should_stop)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _close_downloader(downloader):
    if downloader is None:
        return
    tasks = list(getattr(downloader, "download_tasks", []))
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    close = getattr(downloader, "close", None)
    if close:
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            from . import console
            console.log_event(f"下载器关闭失败：{type(exc).__name__}")


@asynccontextmanager
async def _managed_handler(factory, kwargs):
    """f2 Handler 构造时也会打开下载器，即使仅调用信息接口也要关闭。"""
    handler = factory(kwargs)
    try:
        yield handler
    finally:
        await _close_downloader(getattr(handler, "downloader", None))


def _wrap_error(e: Exception) -> DouyinError:
    """把底层异常翻译成人话。"""
    # 已经是我们自己包装好的（含 LinkParseError）→ 原样透传，避免二次翻译
    if isinstance(e, DouyinError):
        return e

    msg = str(e)
    low = msg.lower()

    if "403" in msg or "uifid" in low or "argus" in low or "forbidden" in low:
        return DouyinError(
            "被抖音风控拦截（403）。请先回到上方「登录抖音」卡片，"
            "点「打开登录窗口」重新登录一次；若刚登录过仍然失败，"
            "说明抖音的风控规则又升级了，需要更新程序版本。"
        )
    if "cookie" in low or "登录" in msg or "login" in low:
        return DouyinError(
            "登录态无效或已失效。请点「打开登录窗口」重新登录一次。"
        )
    if "status_code" in low and ("8" in msg or "empty" in low):
        return DouyinError(
            "接口返回空数据，通常是触发了风控限流。建议：降低下载频率、"
            "更换网络（住宅 IP），或稍后再试。"
        )
    # f2 的已知缺陷（2026-09-29 实测撞上）：**第一页没拿到任何作品**时，
    # 它会在 `handler.py:421-425` 的 `if not video.has_aweme: break` 跳出循环，
    # 而循环外`:455` 的 Bark 通知要读一个从未赋值的局部变量 `nickname_raw`
    # → UnboundLocalError。触发场景有两种：
    #   ① 传了非 0 的 min_cursor（我们已在调用处规避）；
    #   ② 抖音返回空数据（风控限流 / 主页被清空 / 真的没有作品）。
    # 这条错误信息对用户毫无意义，且真实原因是"没拿到数据"，必须翻译。
    if "nickname_raw" in msg:
        return DouyinError(
            "没有从抖音取到任何作品数据。常见原因：① 抖音暂时限制了请求"
            "（接口返回空内容，通常等几分钟或换个网络再试）；"
            "② 该主页没有公开作品。"
        )
    if "timeout" in low or "timed out" in low:
        return DouyinError("网络请求超时。请检查网络或代理设置后重试。")
    # ⚠️ 这里刻意**不再**用 "aweme_id" in msg 来判定「解析失败」——
    #    那会把下游无关异常（如 f2 内部的 KeyError('aweme_id')）也误报成解析问题。
    #    解析类文案现在只由 LinkParseError 承担。
    if "未在响应的地址中找到" in msg:
        return DouyinError("无法从链接中解析出作品 ID，请确认链接是否正确、是否为作品页。")
    if "sec_user_id" in low:
        return DouyinError("无法从链接中解析出用户 ID，请确认是主页链接。")

    return DouyinError(f"下载失败：{msg}")


# ---------------------------------------------------------------------------
# 单视频下载
# ---------------------------------------------------------------------------
async def download_one(
    url_or_id: str,
    cfg: Dict[str, Any],
    on_progress: ProgressCB = None,
    skip_downloaded: bool = True,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """下载单个作品（视频）。

    返回 {"aweme_id":..., "nickname":..., "desc":..., "path":..., "skipped":bool}
    """
    _check_stop(should_stop)
    _prepare_f2_env()

    from f2.apps.douyin.handler import DouyinHandler
    from f2.apps.douyin.utils import AwemeIdFetcher

    def report(cur: int, total: int, msg: str) -> None:
        if on_progress:
            on_progress(cur, total, msg)

    report(0, 3, "解析链接中...")

    # 1) 链接 → aweme_id：先本地正则抠（覆盖 modal_id 等查询参数形态），
    #    抠不到才联网（基本只剩 v.douyin.com 短链需要跟随跳转）
    raw = (url_or_id or "").strip()
    aweme_id = extract_aweme_id(raw)
    if not aweme_id:
        try:
            aweme_id = await AwemeIdFetcher.get_aweme_id(normalize_douyin_url(raw))
        except Exception as e:  # noqa: BLE001
            raise LinkParseError(
                "无法从链接中解析出作品 ID。请确认链接正确、并且是某条作品的页面；"
                "也可以把完整分享文本整段粘进来。"
            ) from e
    aweme_id = str(aweme_id)

    download_dir = cfg_mod.resolve_download_dir(cfg)

    # 2) 增量：★ 只有「记录里的文件此刻真的还在磁盘上」才算已下载。
    #    用户手动删掉 mp4 后会判为 stale → 重新下载，而不是被误判成「已下载过」。
    status = cfg_mod.history_status(download_dir, "one", aweme_id)
    if skip_downloaded and status == "ok":
        report(3, 3, "本地已存在该视频，跳过下载")
        return {
            "aweme_id": aweme_id,
            "skipped": True,
            "reason": "file_exists",
            "path": cfg_mod.recorded_path(download_dir, "one", aweme_id),
        }
    redownload = (status == "stale")
    if redownload:
        report(0, 3, "记录显示曾下载过，但文件已不在，重新下载...")

    report(1, 3, f"获取作品 {aweme_id} 的信息...")

    kwargs = build_kwargs(cfg, mode="one")
    kwargs["_force"] = not skip_downloaded
    kwargs["_should_stop"] = should_stop
    async with _managed_handler(DouyinHandler, kwargs) as handler:
        try:
            detail = await run_cancellable(handler.fetch_one_video(aweme_id=aweme_id), should_stop)
        except Exception as e:  # noqa: BLE001
            raise _wrap_error(e) from e

        data = detail._to_dict()
        nickname = data.get("nickname") or "unknown"
        desc = data.get("desc") or ""

        report(2, 3, "下载视频中...")

        # 3) 调用 f2 下载器落盘（返回 None 表示这次没生成任何文件）
        saved = await _save_video(kwargs, data, Path(kwargs["path"]),
                                  nickname=nickname, desc=desc)
        if not saved:
            raise DouyinError("没有生成视频文件（可能为图文作品、私密作品或下载失败）。")

        # 4) 记账：只记真实落盘的路径。
        #    没有文件就不记，否则下次又会重演「删了还说已下载」。
        if saved:
            cfg_mod.mark_downloaded(download_dir, "one", {aweme_id: saved})

        report(3, 3, "完成" if saved else "没有生成视频文件")
        return {
            "aweme_id": aweme_id,
            "nickname": nickname,
            # desc 是**用于文件名的**短文案；界面展示请用 desc_full（原文案）
            "desc": data.get("desc") or desc,
            "desc_full": data.get("desc_full") or desc,
            "path": saved,
            "skipped": False,
            "redownload": redownload,
        }


# ---------------------------------------------------------------------------
# 主页批量下载
# ---------------------------------------------------------------------------
async def download_user_posts(
    url_or_id: str,
    cfg: Dict[str, Any],
    on_progress: ProgressCB = None,
    max_counts: Optional[int] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    skip_downloaded: bool = True,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """下载某用户主页的作品。

    参数：
      max_counts    最多下载多少条（None/0 表示不限，默认取配置的 20）
      date_start/end 日期区间 "YYYY-MM-DD"，用于筛选
      should_stop   返回 True 时中断（供取消功能）
    """
    _check_stop(should_stop)
    date_start, date_end = validation.date_range(date_start, date_end)
    _prepare_f2_env()

    from f2.apps.douyin.handler import DouyinHandler
    from f2.apps.douyin.utils import SecUserIdFetcher

    def report(cur: int, total: int, msg: str) -> None:
        if on_progress:
            on_progress(cur, total, msg)

    report(0, 1, "解析主页链接...")

    raw = (url_or_id or "").strip()
    sec_user_id = extract_sec_user_id(raw)
    if not sec_user_id:
        try:
            sec_user_id = await SecUserIdFetcher.get_sec_user_id(
                normalize_douyin_url(raw))
        except Exception as e:  # noqa: BLE001
            raise LinkParseError(
                "无法从链接中解析出用户 ID。请确认是该作者的主页链接"
                "（形如 https://www.douyin.com/user/MS4wLjAB...）。"
            ) from e

    download_dir = cfg_mod.resolve_download_dir(cfg)
    scope = "post:" + sec_user_id

    # 上限：显式传入优先，否则用配置默认值
    if max_counts is None:
        max_counts = validation.nonnegative_int(cfg.get("default_max_counts"), default=20)
    else:
        max_counts = validation.nonnegative_int(max_counts)

    kwargs = build_kwargs(cfg, mode="post")
    kwargs["_force"] = not skip_downloaded
    kwargs["_should_stop"] = should_stop
    async with _managed_handler(DouyinHandler, kwargs) as handler:

        report(0, 1, "获取作品列表...")

        downloaded: List[Dict[str, Any]] = []
        skipped: List[str] = []
        resumed: List[str] = []          # 记录在、但文件已丢失 → 本次补下
        failed: List[Dict[str, str]] = []
        seen: set[str] = set()
        index = 0
        pages = 0                        # 已拉取页数（每页最多 20 条）
        # 兜底护栏：有日期区间时 f2 不限量翻页，万一区间设置异常（未来日期等），
        # 最多也只会拉 50 页 ≈ 1000 条，不至于把整个账号翻穿。
        _PAGE_HARD_LIMIT = 50
        past_range = False               # 本页已出现早于起始日期的作品 → 页末收手

        # ---- 日期区间 → f2 的 cursor 边界 ----
        # 背景（2026-09-29 修复一次真实故障）：
        #   用户设「起始 2026-09-28 / 结束 2026-09-29」，结果仍下满 20 条
        #   （最早 2026-08-15）。原因是 **f2 的 create_time 不是 unix 时间戳**：
        #   `UserPostFilter.create_time`（filter.py:160）返回的是
        #   `timestamp_2_str()` 格式化后的**字符串**（默认 "%Y-%m-%d %H-%M-%S"），
        #   而旧版 `_in_date_range()` 一上来就 `int(create_time)` → ValueError →
        #   被 `except (TypeError, ValueError): return True` 吞掉 → 每条都"通过",
        #   过滤器形同虚设。文件名里的 `2026-08-15 11-53-15` 正是这个字符串。
        # 现在两头都补：①服务端用 cursor 收窄请求范围（少翻无用页）；
        #              ②本地逐条过滤兜底（已修好解析）。
        interval = None
        if date_start and date_end:
            interval = f"{date_start}|{date_end}"
        elif date_start:
            interval = f"{date_start}|2099-12-31"
        elif date_end:
            interval = f"2000-01-01|{date_end}"

        f2_max_cursor = 0
        if interval:
            from f2.utils.utils import interval_2_timestamp

            # 结束边界：作为第一页的起点游标（抖音语义 = 取该时间之前的作品）。
            # 若结束日期盖到今天或未来（含只填起始日期时的 2099），给未来游标没意义，
            # 直接用 0 = 从该作者最新作品开始。
            end_ts = interval_2_timestamp(interval, date_type="end")
            f2_max_cursor = end_ts if end_ts < int(time.time() * 1000) else 0
            report(0, 1, f"按日期筛选：{date_start or '不限'} ~ {date_end or '不限'}")

        # ⚠️ 不要给 f2 传 min_cursor（起始边界）。
        #   看似能用它让 f2 自己「翻过区间起点就收手」，但它会踩中 f2 的一个
        #   UnboundLocalError：`handler.py:417` 的
        #       if max_cursor < min_cursor:  break
        #   只要在第一轮就成立（典型组合 max_cursor=0 + min_cursor=起始时间戳），
        #   while 循环会在 `nickname_raw = video.nickname_raw[0]`（:431）之前退出，
        #   而 `:455` 的 Bark 通知**在循环外**用了这个局部变量 →
        #       cannot access local variable 'nickname_raw' where it is not
        #       associated with a value
        #   （已实测复现：整个任务直接 failed）。
        #   min_cursor 传 0 时该分支永不成立，代价是自己多做收手判断 —— 见下方的
        #   past_range 与 _PAGE_HARD_LIMIT。

        try:
            # max_counts 直接交给 f2 时，它按「收到的条数」计数 —— 区间外的作品
            # 也会吃掉配额，导致区间内明明还有作品却被提前截断。所以有日期区间时
            # 改为不限量（由下面的循环按"区间内作品数"自己卡上限，并在越过区间
            # 起点时收手）；无区间时保持原有语义不变。
            async for page in handler.fetch_user_post_videos(
                sec_user_id,
                min_cursor=0,            # ★ 必须为 0，理由见上方注释（f2 的 nickname_raw 坑）
                max_cursor=f2_max_cursor,
                page_counts=20,
                max_counts=None if interval else (max_counts or None),
            ):
                _check_stop(should_stop)

                pages += 1
                if interval and pages > _PAGE_HARD_LIMIT:
                    report(index, index,
                           f"已达翻页上限（{_PAGE_HARD_LIMIT} 页），停止查找")
                    break

                past_range = False
                items = page._to_list() or []
                for item in items:
                    _check_stop(should_stop)

                    aweme_id = str(item.get("aweme_id") or "")
                    if not aweme_id or aweme_id in seen:
                        continue
                    seen.add(aweme_id)

                    # 日期筛选：作品列表按时间倒序，f2 的 create_time 是格式化字符串
                    # （见函数头注释），把区间外的直接跳过。
                    ct = item.get("create_time")
                    if interval and ct and not _in_date_range(ct, date_start, date_end):
                        # 已经早于起始日期 → 后面的只会更早，本页跑完就收手
                        if _before_range(ct, date_start):
                            past_range = True
                        continue

                    # 增量：★ 只有文件此刻真的还在磁盘上才跳过
                    status = cfg_mod.history_status(download_dir, scope, aweme_id)
                    if skip_downloaded and status == "ok":
                        skipped.append(aweme_id)
                        index += 1
                        report(index, max_counts or index, f"已跳过 {index} 条")
                        # ⚠️ 这里必须自己再判一次条数上限：下面的 continue 会跳过
                        #    循环体末尾的统一检查，否则被跳过的条数不计入上限，
                        #    会多下一条（历史遗留 bug）。
                        if max_counts and len(downloaded) + len(skipped) + len(failed) >= max_counts:
                            break
                        continue
                    is_resume = (status == "stale")

                    nickname = item.get("nickname") or "unknown"
                    desc = item.get("desc") or ""
                    try:
                        saved = await _save_video(kwargs, item, Path(kwargs["path"]),
                                                  nickname=nickname, desc=desc)
                        if not saved:
                            raise DouyinError("没有生成视频文件（可能为图文作品、私密作品或下载失败）。")
                        if saved:
                            cfg_mod.mark_downloaded(download_dir, scope,
                                                    {aweme_id: saved})
                            if is_resume:
                                resumed.append(aweme_id)
                        downloaded.append({
                            "aweme_id": aweme_id,
                            "nickname": nickname,
                            # desc 已用于文件名；desc_full 保留原文案供界面展示
                            "desc": item.get("desc") or desc,
                            "desc_full": item.get("desc_full") or desc,
                            "path": saved,
                            "redownload": is_resume,
                        })
                    except (DownloadCancelled, cfg_mod.PersistenceError):
                        raise
                    except Exception as e:  # noqa: BLE001
                        failed.append({"aweme_id": aweme_id, "error": str(e)})

                    index += 1
                    report(index, max_counts or index, f"已处理 {index} 条")

                    # 条数上限
                    if max_counts and len(downloaded) + len(skipped) + len(failed) >= max_counts:
                        break

                if max_counts and len(downloaded) + len(skipped) + len(failed) >= max_counts:
                    break

                if past_range:
                    report(index, index, "已越过起始日期，停止查找")
                    break

        except Exception as e:  # noqa: BLE001
            raise _wrap_error(e) from e

        _check_stop(should_stop)
        report(index, index, "完成")
        return {
            "sec_user_id": sec_user_id,
            "downloaded": downloaded,
            "skipped": skipped,
            "resumed": resumed,
            "failed": failed,
            "total_handled": index,
            "download_count": len([d for d in downloaded if d.get("path")]),
            # 回传筛选条件与实翻页数，好让界面在「0 条」时说清原因，
            # 而不是只甩一句"下载 0 条"让人猜（日期区间是最常见的落空原因）。
            "date_start": date_start,
            "date_end": date_end,
            "pages_fetched": pages,
        }


def _parse_create_date(create_time: Any) -> "Any":
    """把 f2 的 create_time 解析成 `datetime.date`，解析不了返回 None。

    ⚠️ 2026-09-29 修复的真实故障（日期筛选完全失效）：
      旧实现假设 `create_time` 是 unix 时间戳，直接 `int()`。但 f2 的
      `UserPostFilter.create_time`（filter.py:160-165）返回的是
      `timestamp_2_str()` 格式化后的**字符串**（默认 "%Y-%m-%d %H-%M-%S"，
      例："2026-08-15 11-53-15"）—— 这正是文件名里那串时间。
      `int("2026-08-15 11-53-15")` 抛 ValueError，被上层 `except` 吞掉后
      `return True`，于是**每一条都被判定为"在区间内"**，用户设了 09-28~09-29
      照样下满 20 条（最早 08-15）。
      这里把两种形态都认下来：数字（秒/毫秒时间戳）与常见日期字符串。
    """
    import datetime

    if create_time is None or isinstance(create_time, bool):
        return None

    # 形态 1：数字时间戳
    if isinstance(create_time, (int, float)):
        ts = float(create_time)
        if ts > 1e11:            # 毫秒 → 秒
            ts /= 1000.0
        try:
            # 与 f2 的 timestamp_2_str 对齐，统一按东八区取日期
            tz_cn = datetime.timezone(datetime.timedelta(hours=8))
            return datetime.datetime.fromtimestamp(ts, tz_cn).date()
        except (OSError, OverflowError, ValueError):
            return None

    s = str(create_time).strip()
    if not s:
        return None

    # 形态 2：纯数字字符串（秒/毫秒时间戳）
    if re.fullmatch(r"\d{9,14}", s):
        return _parse_create_date(int(s))

    # 形态 3：f2 的默认格式优先，再兜常见变体
    for fmt in ("%Y-%m-%d %H-%M-%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # 兜底：取开头 10 个字符当 ISO 日期
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _before_range(create_time: Any, date_start: Optional[str]) -> bool:
    """作品是否**早于**起始日期（用于倒序列表的提前收手判断）。"""
    import datetime

    if not date_start:
        return False
    d = _parse_create_date(create_time)
    if d is None:
        return False
    try:
        return d < datetime.date.fromisoformat(date_start)
    except ValueError:
        return False


def _in_date_range(create_time: Any, date_start: Optional[str],
                   date_end: Optional[str]) -> bool:
    """判断作品创建时间是否落在区间内。

    create_time 既可能是 unix 时间戳（旧接口/原始数据），
    也可能是 f2 格式化后的字符串（`_to_list()` 的实际情况）——
    统一走 `_parse_create_date()`。
    解析失败时**放行**（宁可多留，不可误杀），但这属于异常路径：
    正常数据一定能解析出来。
    """
    import datetime

    dt = _parse_create_date(create_time)
    if dt is None:
        return True

    if date_start:
        try:
            if dt < datetime.date.fromisoformat(date_start):
                return False
        except ValueError:
            pass
    if date_end:
        try:
            if dt > datetime.date.fromisoformat(date_end):
                return False
        except ValueError:
            pass
    return True


# ---------------------------------------------------------------------------
# 落盘（复用 f2 的下载器）
# ---------------------------------------------------------------------------
# f2 生成的媒体文件扩展名（用于兜底扫描）
_MEDIA_EXTS = {".mp4", ".webm", ".mov", ".m4v"}

# ---------------------------------------------------------------------------
# 文案截断：desc 只取前 N 个字
# ---------------------------------------------------------------------------
# 背景（2026-09-28 定位）：f2 的 `format_file_name()` 默认用
#   `split_filename(desc, os_limit)` 处理文案，逻辑是
#     split_index = min(total_length, 200) // 2 - 6      # win32 → 94
#     text[:94] + "......" + text[-94:]
#   注意它**按加权长度判定超限**（中文算 3 字符），却**按纯字符数截前 94 个**。
#   于是一个 190 个中文字的文案会被算成 570 → 超限 → 截成
#     「前 94 字 + ...... + 后 94 字」= 194 字符，
#   再拼上 `_video.mp4` 后缀 → 整段路径逼近 260 字符的 Windows 上限。
#   实测后果：f2 落盘出一个 **截断且没有扩展名** 的残废文件
#   （`Download/喵喵折/2026-03-05 10-55-03_喵喵折_2026全网最细心的...`，
#   恰好 111 字符、结尾 `...咱们要做`），播放器打不开、资源管理器显示 0 字节。
#
# 对策：不跟 f2 的截断逻辑掰扯，直接在**下载前**把 data["desc"] 削短。
#   f2 的 `download_video()`（dl.py:203）调 `format_file_name` 时不传
#   `custom_fields`，所以没法用参数注入 —— 只能改数据本身。
#   好在我们的 `expected_media_paths()` 用的是同一份 data，路径预判天然一致。
_DESC_MAX_CHARS = 6

# 数「前 6 个字」之前先剔除的噪声字符：
#   # @ 话题/艾特符号、常见表情与变体选择符、零宽字符 —— 它们占位但没信息量。
# 例：#笔记本电脑 #换硅脂 → 剔除后「笔记本电脑换硅脂」→ 取前 6 字「笔记本电脑换硅」
_DESC_NOISE_RE = re.compile(
    "[#＃@＠\u200b-\u200f\ufe0e\ufe0f\u2600-\u27bf\U0001f000-\U0001faff]"
)


def _short_desc(data: Dict[str, Any], max_chars: int = _DESC_MAX_CHARS) -> Dict[str, Any]:
    """原地把 `data["desc"]` 削成「前 max_chars 个正文字」，返回同一个 dict。

    处理顺序：**先剔噪声 → 再截字 → 最后清洗**。
    若反过来（先清洗再截），`#笔记本电脑` 会变成 `_笔记本电`，
    开头多一个下划线，实际只留下 5 个正文字。

    - 保留原文案到 `data["desc_full"]`，供界面展示/日志使用
    - 剔除 `#`/`@`/表情后继续数满 max_chars 个字
    - 空白不算「字」：`笔记本电脑 换硅脂` 取 6 字得 `笔记本电脑换硅`
    - max_chars <= 0 表示不截断
    """
    if max_chars <= 0 or not isinstance(data, dict):
        return data
    raw = str(data.get("desc") or "").strip()
    if not raw:
        return data
    data.setdefault("desc_full", raw)

    # ① 剔除话题符号、表情、所有空白 —— 只留下真正有信息量的字符
    cleaned_text = _DESC_NOISE_RE.sub("", raw)
    cleaned_text = re.sub(r"\s+", "", cleaned_text)
    if not cleaned_text:
        cleaned_text = raw          # 整条都是符号（罕见）→ 退回原文

    # ② 取前 max_chars 个字
    head = cleaned_text[:max_chars]

    # ③ 清洗成合法文件名片段
    cleaned = sanitize_filename(head, max_chars)
    if cleaned == "untitled":
        cleaned = sanitize_filename(cleaned_text, max_chars) or "untitled"
    data["desc"] = cleaned
    return data


def expected_media_paths(user_dir: Path, data: Dict[str, Any],
                         naming: str) -> List[Path]:
    """**下载前**就算出 f2 将要落盘的文件路径。

    原理：f2 的 `dl.py::download_video()` 里文件名是
        format_file_name(kwargs["naming"], data) + "_video" + ".mp4"
    `format_file_name` 是 f2 的公开函数，用同一份 data 调用就能得到同一个名字。

    有了它就能解决两个问题：
    1. 精确判断「文件到底在不在」，从而实现「文件被删就重新下载」；
    2. f2 判定文件已存在时会直接跳过、不产生新文件（此时目录快照差集为空），
       旧实现只能返回目录路径 —— 现在能直接给出真实文件路径。

    返回候选路径列表（可能为空，例如 data 缺字段或模板非法）。
    """
    try:
        from f2.apps.douyin.utils import format_file_name

        base = format_file_name(naming or "{create}_{nickname}_{desc}", data)
    except Exception:  # noqa: BLE001
        return []
    if not base:
        return []

    candidates = [
        user_dir / f"{base}_video.mp4",       # 普通视频（主力）
        user_dir / f"{base}_live_1.mp4",      # 实况图集的视频段
        user_dir / f"{base}_live_1.webm",
    ]
    return candidates


async def _save_video(kwargs: Dict[str, Any], data: Dict[str, Any],
                      base_dir: Path, nickname: str, desc: str) -> Optional[str]:
    """调用 f2 的 downloader 把视频存到磁盘，返回**真实文件路径**。

    返回 None 表示「这次没有生成任何文件」（作品被屏蔽 / 私密 / 纯图集等）。
    ⚠️ 早期版本在没有新文件时会返回**目录**，导致上层把它当文件记进下载历史，
       于是「文件被删了还说已下载」。现在绝不会再返回目录。

    f2 的 `handler_download()` **不返回路径**，所以我们需要自己推断：
    先按 naming 模板预判 → 再用下载前后快照差集 → 最后按基名 glob 兜底。
    """
    from f2.apps.douyin.dl import DouyinDownloader

    # ★ 先把文案削短，再算路径 —— f2 的下载器与我们的 expected_media_paths
    #   读的是同一个 dict，两边必须看到完全一样的 desc，否则路径预判会落空。
    _short_desc(data)

    # 每个作者一个子目录
    user_dir = base_dir / sanitize_filename(nickname, 40)

    try:
        user_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DouyinError(f"无法创建下载目录 {user_dir}：{e}") from e

    # ★ 下载前预判目标路径（f2 若判定「已存在」会静默跳过，不会产生新文件）
    candidates = expected_media_paths(user_dir, data, kwargs.get("naming") or "")

    # 记录下载前的 mp4 快照，用于事后识别新文件
    before = _snapshot_mp4(user_dir)

    # 强制重下使用全新的目录，绕过 f2 对已有文件的跳过逻辑。
    # 成功前不删除/覆盖旧视频，失败或取消时自动清理临时目录。
    staging = (tempfile.TemporaryDirectory(prefix=".redownload-", dir=user_dir)
               if kwargs.get("_force") else None)
    transfer_dir = Path(staging.name) if staging else user_dir
    if staging:
        candidates = expected_media_paths(transfer_dir, data, kwargs.get("naming") or "")
        before = set()

    downloader = None
    try:
        downloader = DouyinDownloader(kwargs)
        await run_cancellable(
            downloader.handler_download(kwargs, data, transfer_dir), kwargs.get("_should_stop"))
        # ★ handler_download 内部只是把下载任务 `asyncio.create_task` 压进队列，
        #   真正的传输发生在 execute_tasks()。漏掉这一句的典型症状：
        #   作者目录建好了，但里面一个文件都没有（下载"静默失败"）。
        await run_cancellable(downloader.execute_tasks(), kwargs.get("_should_stop"))

        # 优先精确命中，再使用本次新增的媒体文件。零字节文件不算成功。
        saved = next((p for p in candidates if p.is_file() and p.stat().st_size > 0), None)
        if saved is None:
            new_files = sorted(_snapshot_mp4(transfer_dir) - before)
            saved = next((transfer_dir / name for name in new_files
                          if (transfer_dir / name).stat().st_size > 0), None)
        if saved is None and candidates:
            stem = candidates[0].name[:-len("_video.mp4")]
            saved = next((p for p in sorted(transfer_dir.glob(f"{stem}*"))
                          if p.is_file() and p.suffix.lower() in _MEDIA_EXTS
                          and p.stat().st_size > 0), None)
        _check_stop(kwargs.get("_should_stop"))
        if saved is not None and staging:
            destination = user_dir / saved.name
            # 临时目录与目标同盘，替换单个文件是原子的。
            for media in transfer_dir.iterdir():
                if media.is_file() and media.suffix.lower() in _MEDIA_EXTS and media.stat().st_size > 0:
                    os.replace(media, user_dir / media.name)
            saved = destination
        return str(saved) if saved else None
    finally:
        # f2 会创建独立 asyncio.Task，取消父协程后也必须收回它们。
        try:
            await _close_downloader(downloader)
        finally:
            if staging:
                staging.cleanup()


def _snapshot_mp4(folder: Path) -> set[str]:
    """列出目录下所有媒体文件名（仅名称，便于差集比较）。"""
    exts = {".mp4", ".webm", ".mov", ".m4v"}
    try:
        return {p.name for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in exts}
    except OSError:
        return set()


# ---------------------------------------------------------------------------
# 链接解析预览（不下载，只告诉用户这是啥）
# ---------------------------------------------------------------------------
async def parse_link_info(url: str) -> Dict[str, Any]:
    """解析链接，返回类型与目标 ID，供界面预览。

    返回 {"kind": "video"|"user", "id": "...", "label": "...", "url": "规范地址"}
    其中 url 已抹掉原链接里那些无关的查询参数，便于界面展示。
    """
    _prepare_f2_env()

    from f2.apps.douyin.utils import AwemeIdFetcher, SecUserIdFetcher

    raw = (url or "").strip()

    def result(kind: str, target_id: str, label: str) -> Dict[str, Any]:
        base = ("https://www.douyin.com/video/"
                if kind == "video" else "https://www.douyin.com/user/")
        return {
            "kind": kind,
            "id": str(target_id),
            "label": label,
            "url": base + str(target_id),
        }

    # 1) 纯数字 → 作品 ID
    if raw.isdigit():
        return result("video", raw, f"作品 {raw}")

    # 2) 本地先抠作品 ID（不联网）。
    #    必须排在「主页」判断之前：/user/MS4wLjABxxx?modal_id=7xxx 这种地址
    #    是「在主页里点开了某个作品」，用户想要的是那条作品，不是整个主页。
    aid = extract_aweme_id(raw)
    if aid:
        return result("video", aid, f"作品 {aid}")

    # 3) 本地抠 sec_user_id（不联网）
    sid = extract_sec_user_id(raw)
    if sid:
        return result("user", sid, f"用户主页 {sid[:20]}...")

    # 4) 含 /user/ 或 sec_uid 参数 → 主页（需联网跟随跳转）
    if "/user/" in raw or "sec_uid" in raw:
        try:
            sid = await SecUserIdFetcher.get_sec_user_id(raw)
            return result("user", sid, f"用户主页 {sid[:20]}...")
        except Exception as e:  # noqa: BLE001
            raise DouyinError(f"解析用户主页失败：{e}") from e

    # 5) 短链：跟随跳转判断类型
    try:
        if "v.douyin.com" in raw:
            # 短链可能是作品也可能是主页，先尝试作品
            try:
                aid = await AwemeIdFetcher.get_aweme_id(raw)
                return result("video", aid, f"作品 {aid}")
            except Exception:  # noqa: BLE001
                sid = await SecUserIdFetcher.get_sec_user_id(raw)
                return result("user", sid, "用户主页")

        aid = await AwemeIdFetcher.get_aweme_id(normalize_douyin_url(raw))
        return result("video", aid, f"作品 {aid}")
    except Exception as e:  # noqa: BLE001
        raise DouyinError(f"无法识别的链接：{e}") from e


# ---------------------------------------------------------------------------
# 便捷同步入口
# ---------------------------------------------------------------------------
def run_async(coro):
    """在同步环境里跑一个协程（CLI 用）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已在事件循环里（如 Flask 的异步场景）则另开线程
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
