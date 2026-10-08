# -*- coding: utf-8 -*-
"""把界面装进「我们自己的窗口」：无边框 + 原生四边缩放。

为什么要这个模块
----------------
界面以前是 `webbrowser.open(url)` 打开系统浏览器，必然是「标签栏 + 地址栏 +
窗口标题栏」的完整浏览器窗口。想让它无边框，唯一手段是从外部改浏览器窗口的
Win32 样式（剥 `WS_CAPTION`）—— 这条路实测已经废了：**Chrome/Edge 会检测到
样式被改并立刻改回去**（Superuser 上有 Window Detective 的实测记录），
`--disable-features=Windows10CustomTitlebar` 也早已失效。

所以改成「自己开窗」：用 pywebview（Edge WebView2 渲染）创建窗口，
窗口归本进程所有，没有第二个进程来抢样式。

无边框 + 能缩放，是怎么同时拿到的（每格都有实测数据）
------------------------------------------------------
| 做法                                         | 非客户区      | 四边缩放        |
| ---                                          | ---           | ---             |
| `frameless=True`                             | 0x0 真无边框  | 否，全 HTCLIENT |
| `frameless=True` + 加回 `WS_THICKFRAME`      | 7px           | 是（原生）      |
| 再拦 `WM_NCCALCSIZE` 返回 0                  | 0x0           | 否（系统按客户区算） |
| **上面前三条叠起来 + 自己算 `WM_NCHITTEST`** | **0x0**       | **是（原生）**  |

最后一块拼图：拦了 `WM_NCCALCSIZE` 之后系统不再给缩放边，那就**自己在
`WM_NCHITTEST` 里按窗口边缘算**。Electron / Chromium 的自绘窗口就是这么做的。

安全边界：子类化窗口过程只对自己进程创建的窗口做（`SetWindowLongPtrW` 跨进程本来
也不允许），不碰任何外部程序。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import console

_IS_WIN = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
GWL_STYLE = -16
GWLP_WNDPROC = -4

WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_MAXIMIZE = 0x01000000
WS_MINIMIZE = 0x20000000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

SW_RESTORE = 9

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_GETMINMAXINFO = 0x0024

HTCLIENT = 1
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT = 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

RESIZE_BORDER = 8      # 四边缩放热区（物理像素）
RESIZE_CORNER = 18     # 四角缩放热区

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2
DWMWA_COLOR_NONE = 0xFFFFFFFE       # 去掉 Win11 那条 1px 描边

MONITOR_DEFAULTTONEAREST = 2
SPI_GETWORKAREA = 0x0030

# 默认窗口尺寸 / 最小尺寸（逻辑像素）
DEFAULT_W, DEFAULT_H = 1180, 860
MIN_W, MIN_H = 900, 600

# 后台线程等窗口出现的最长时间（秒）。首次冷启动 WebView2 会慢一些。
_ATTACH_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Win32 结构体（ctypes.wintypes 里没有这两个，自己定义）
# ---------------------------------------------------------------------------
class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


# ---------------------------------------------------------------------------
# ctypes 绑定（全部是系统 DLL，不新增任何第三方依赖）
# ---------------------------------------------------------------------------
if _IS_WIN:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                          ctypes.c_ssize_t]
    _user32.CallWindowProcW.restype = ctypes.c_ssize_t
    _user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, wintypes.HWND,
                                        ctypes.c_uint, ctypes.c_size_t,
                                        ctypes.c_ssize_t]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.MonitorFromWindow.restype = ctypes.c_void_p
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(MONITORINFO)]
    _user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsIconic.argtypes = [wintypes.HWND]

    _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                 ctypes.c_size_t, ctypes.c_ssize_t)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                      ctypes.c_ssize_t)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, ctypes.c_ssize_t]


# ---------------------------------------------------------------------------
# 窗口过程：保活对象（回调被 GC 掉会让窗口崩）
# ---------------------------------------------------------------------------
_HOOKS: List[Any] = []          # 保活 SetWindowLongPtrW 传进去的回调
_STATE: dict = {"hwnd": None, "orig": 0, "proc": None, "hooked": False,
                "window": None}
_focus_cb: Optional[Callable[[], None]] = None


# ---------------------------------------------------------------------------
# 启动握手：窗口创建 → 页面首帧
# ---------------------------------------------------------------------------
# 窗口是「先出现、后画内容」的：实测窗口 2.5s 就显示出来了，而 WebView2 要到
# 4s 才就绪、7s 才画出第一帧。这几秒里窗口只铺了 background_color，而那个颜色
# 和页面底色**完全一样**（深色下都是 #1F1E1B）—— 所以用户看到的就是「打开了
# 但什么都没渲染」，很容易判定成程序坏了。
#
# 前端首帧画好后会 POST /api/window/boot。有了这个信号：
#   1. 日志里能留下「窗口创建 → 首帧」的真实耗时（以后有据可查，不用再猜）
#   2. 迟迟收不到信号的实例会被主动重载一次 —— WebView2 偶发加载失败时能自愈
_BOOT_LOCK = threading.Lock()
_BOOT: Dict[str, Any] = {"t0": None, "at": None, "payload": None,
                         "reloads": 0, "revealed": False}

# 等前端「已渲染」信号的最长时间（秒）。冷启动本机实测首帧在 7s 内，
# 留一倍余量；超过这个数基本可以认定是页面没进来。
BOOT_TIMEOUT = 18.0


def reveal_window() -> bool:
    """把（隐藏创建的）窗口显示出来。只有第一次调用生效。

    窗口为什么是隐藏创建的：见 create_window 那段的实测数据 —— 窗口 0.66s 就能
    显示，而 WebView2 首帧要 3.29s，中间那段窗口只有一层和页面底色**完全相同**的
    纯色，看上去就是「打开了但什么都没渲染」。让窗口等页面画好，用户看到的第一眼
    就是完整的界面。

    2026-10-03 补充（实测，别再重复尝试）：也试过「藏 0.6s 就提前 show()」，
    想靠这个让用户早点看到转圈遮罩 —— **无效**。Chromium 对刚变可见的窗口
    还没合成过任何一帧，遮罩不会跟着出现（连拍实测：0.65s 显示，0.65~6.7s
    全是空底色，直到 6.88s 界面才一次性出现；`w.refresh()` 也没用）。
    ⇒ 唯一有效的杠杆是把 WebView2 首帧本身做快。

    触发点有两个：
      - 前端首帧就绪 → /api/window/boot → note_boot() 里调这里（正常路径）
      - 看门狗等超时 → 也调这里强制显示（宁可先给一个还没画好的窗口，
        也不能让用户对着「双击没反应」发呆）
    """
    with _BOOT_LOCK:
        if _BOOT.get("revealed"):
            return False
        _BOOT["revealed"] = True
        t0 = _BOOT["t0"]

    w = _STATE.get("window")
    if w is None:
        console.log_event("该显示窗口了，但窗口对象还是空的（创建失败了？）")
        return False
    try:
        w.show()
    except Exception as e:                      # noqa: BLE001
        console.log_event(f"显示窗口失败：{type(e).__name__}: {e}")
        return False
    if t0:
        console.log_event(f"窗口已显示：创建后 {time.time() - t0:.2f}s")
    try:
        focus_window()                          # 隐藏创建的窗口不会被系统带到前台
    except Exception:                           # noqa: BLE001
        pass
    return True


def note_boot(payload: Optional[Dict[str, Any]] = None) -> bool:
    """前端报告「页面已渲染」。返回 True 表示这是第一次收到（幂等）。"""
    payload = payload or {}
    with _BOOT_LOCK:
        first = _BOOT["at"] is None
        _BOOT["at"] = time.time()
        _BOOT["payload"] = payload
        t0 = _BOOT["t0"]
    if first:
        reveal_window()                         # 页面画好了 → 把窗口亮出来
        dt = f"{time.time() - t0:.2f}s" if t0 else "?"
        errs = payload.get("errors") or []
        extra = ""
        if payload.get("firstRaf") is not None:
            extra = (f"，WebView2 首帧 {payload['firstRaf']}ms / "
                     f"界面就绪 {payload.get('dom')}ms（相对页面导航）")
        msg = (f"页面已渲染：窗口创建后 {dt}"
               f"（触发={payload.get('why')}{extra}，"
               f"卡片 {payload.get('cards')} 个）")
        if errs:
            msg += f"；前端报错 {len(errs)} 条：{errs}"
        console.log_event(msg)
    return first


def boot_done() -> bool:
    with _BOOT_LOCK:
        return _BOOT["at"] is not None


def boot_info() -> Dict[str, Any]:
    """给诊断接口用的启动状态快照。"""
    with _BOOT_LOCK:
        t0, at = _BOOT["t0"], _BOOT["at"]
        return {
            "done": at is not None,
            "revealed": bool(_BOOT.get("revealed")),
            "reloads": _BOOT["reloads"],
            "to_first_paint": (round(at - t0, 2) if (t0 and at) else None),
            "waiting": (round(time.time() - t0, 2) if (t0 and at is None) else None),
        }


def _await_boot(timeout: float) -> bool:
    """等前端报「已渲染」，最多等 timeout 秒。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if boot_done():
            return True
        time.sleep(0.5)
    return boot_done()


def _boot_watchdog(window: Any, url: str) -> None:
    """页面迟迟不报「已渲染」时的兜底。

    正常情况走第一段就返回了。后面两段是给异常兜底的，顺序很重要：

    1) 先等前端的首帧信号（正常路径，实测 4.2s 到）。
    2) 等不到就把窗口**显示出来** —— 窗口本来是隐藏创建的，不显示等于「双击没
       反应」。哪怕页面还没画好，让用户看到窗口也比什么都没有强。
       （10-03 补充：实测这一步显示出来屏幕上仍是空底色 —— Chromium 对刚变可见的
        窗口还没合成过帧，遮罩不会跟着出现。但「显示出来」仍比「一直藏着」好：
        至少用户能看到窗口已经在了，且首帧一到内容立刻填进去。）
    3) 窗口显示出来了页面还是空白，那才轮到重载页面。这一步必须放在窗口显示
       之后：窗口还藏着的时候 GUI 线程多半正在初始化 WebView2，这时调 load_url
       会把初始化拖死（2026-09-09 实测复现过，窗口永远停在 300x300 不可见）。
       窗口能显示出来 = GUI 线程是活的，load_url 才是安全的。
    """
    if _await_boot(BOOT_TIMEOUT):
        return

    console.log_event(f"页面 {BOOT_TIMEOUT:.0f}s 内没报「已渲染」，先把窗口显示出来兜底")
    revealed = reveal_window()
    if revealed and _await_boot(3.0):
        return

    if not revealed:
        console.log_event("窗口没能显示出来（WebView2 初始化可能卡住了），不重载页面")
        return

    with _BOOT_LOCK:
        n = _BOOT["reloads"]
    if n >= 1:
        console.log_event(
            f"重载后仍未收到渲染信号 —— 窗口是显示着的，可手动刷新"
            f"（Ctrl+Shift+R）再看一次")
        return

    with _BOOT_LOCK:
        _BOOT["reloads"] = n + 1
    console.log_event("窗口已显示但页面无渲染信号，主动重载一次页面")
    try:
        window.load_url(url)
    except Exception as e:                      # noqa: BLE001
        console.log_event(f"重载页面失败：{type(e).__name__}: {e}")
        return
    if _await_boot(BOOT_TIMEOUT):
        console.log_event("重载后页面已渲染")
    else:
        console.log_event("重载后仍未收到渲染信号 —— 可手动刷新（Ctrl+Shift+R）再看一次")


def _hit_test(hwnd: int, lparam: int) -> Optional[int]:
    """按窗口边缘算缩放热区；不在热区就返回 None（交回系统 = HTCLIENT）。"""
    if _is_zoomed(hwnd):
        return None             # 最大化状态不给缩放边，避免把窗口拖成还原态

    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value

    r = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None

    left, top = x - r.left, y - r.top
    right, bottom = r.right - x, r.bottom - y
    if min(left, top, right, bottom) < 0:
        return None             # 点在窗口外，别抢系统的判断

    b, c = RESIZE_BORDER, RESIZE_CORNER
    if left < c and top < c:
        return HTTOPLEFT
    if right < c and top < c:
        return HTTOPRIGHT
    if left < c and bottom < c:
        return HTBOTTOMLEFT
    if right < c and bottom < c:
        return HTBOTTOMRIGHT
    if left < b:
        return HTLEFT
    if right < b:
        return HTRIGHT
    if top < b:
        return HTTOP
    if bottom < b:
        return HTBOTTOM
    return None


def _work_area(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """窗口所在显示器的工作区（屏幕坐标，已排除任务栏）。"""
    mon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not mon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return None
    return (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)


def _is_zoomed(hwnd: int) -> bool:
    """是否处于最大化。IsZoomed 之外再看一眼样式位，避免消息时序上的空档。"""
    try:
        if _user32.IsZoomed(hwnd):
            return True
        style = int(_user32.GetWindowLongPtrW(hwnd, GWL_STYLE))
        return bool(style & WS_MAXIMIZE) and not bool(style & WS_MINIMIZE)
    except Exception:                           # noqa: BLE001
        return False


def _fix_maximize(hwnd: int, lparam: int) -> None:
    """客户区 = 整窗之后，最大化会盖住任务栏；这里夹回工作区。"""
    try:
        mmi = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
        wa = _work_area(hwnd)
        if not wa:
            return
        mon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return
        left, top, right, bottom = wa
        mmi.ptMaxPosition.x = left - mi.rcMonitor.left
        mmi.ptMaxPosition.y = top - mi.rcMonitor.top
        mmi.ptMaxSize.x = right - left
        mmi.ptMaxSize.y = bottom - top
    except Exception:           # noqa: BLE001
        pass


def _wnd_proc(hwnd, msg, wparam, lparam):       # noqa: ANN001
    """我们自己的窗口过程：只拦 3 个消息，其余原样转发。"""
    try:
        if msg == WM_NCCALCSIZE and wparam:
            if _is_zoomed(hwnd):
                # 最大化时系统按「窗口有边框」算尺寸：窗口 = 工作区 + 四边 8px 边框，
                # 边框溢到屏幕外（原生有边框窗口就是这样，看不见边框）。
                # 但我们的客户区 = 整窗，于是页面会比工作区大 16px、底部压住任务栏。
                # 所以只在最大化这一种情况下把客户区夹回工作区 ——
                # Chromium 自绘窗口（HWNDMessageHandler）也是这么处理的。
                wa = _work_area(hwnd)
                if wa:
                    rgrc = ctypes.cast(lparam,
                                       ctypes.POINTER(wintypes.RECT)).contents
                    rgrc.left, rgrc.top, rgrc.right, rgrc.bottom = wa
            return 0                            # 客户区 = 整窗 → 四周零留白
        if msg == WM_NCHITTEST:
            code = _hit_test(hwnd, lparam)
            if code is not None:
                return code
        elif msg == WM_GETMINMAXINFO:
            _fix_maximize(hwnd, lparam)
    except Exception:                           # noqa: BLE001
        pass                                    # 绝不因为回调异常把窗口弄死
    return _user32.CallWindowProcW(_STATE["orig"], hwnd, msg, wparam, lparam)


# ---------------------------------------------------------------------------
# 找窗口 / 加固窗口
# ---------------------------------------------------------------------------
def _own_windows(visible_only: bool = True) -> List[Tuple[int, str, int]]:
    """枚举本进程的顶层窗口：(hwnd, 标题, 面积)。

    :param visible_only: 默认只收可见窗口。窗口改成「先隐藏、等页面画好再显示」
        之后，加固线程需要在**显示之前**就找到它 —— 那时它还是隐藏的，所以要
        能带上不可见的那一批。
    """
    me = os.getpid()
    found: List[Tuple[int, str, int]] = []

    def cb(hwnd, _):                            # noqa: ANN001
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me and (not visible_only or _user32.IsWindowVisible(hwnd)):
            buf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, buf, 512)
            r = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((int(hwnd), buf.value,
                          (r.right - r.left) * (r.bottom - r.top)))
        return True

    _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


def _native_hwnd(window) -> Optional[int]:      # noqa: ANN001
    """优先用 pywebview 自己给的原生句柄，拿不到就按进程枚举。"""
    for attr in ("native", "gui"):
        obj = getattr(window, attr, None)
        handle = getattr(obj, "Handle", None)
        if handle is not None:
            try:
                return int(handle.ToInt64())
            except AttributeError:
                return int(handle)
    return None


def find_window(title: str) -> Optional[int]:
    hwnd = None
    import webview
    if webview.windows:
        hwnd = _native_hwnd(webview.windows[0])
    if hwnd:
        return hwnd
    cands = _own_windows()
    if not cands:
        return None
    for h, t, _a in cands:                      # 标题对得上最准
        if t and (title in t or t in title):
            return h
    return max(cands, key=lambda c: c[2])[0]    # 退而求其次：最大的那个


def find_window_safe(title: str, visible_only: bool = True) -> Optional[int]:
    """纯 Win32 枚举找本进程的顶层窗口 —— **不碰 pywebview 的 GUI 对象**。

    和 find_window() 的唯一区别就在这里，但差别是致命的：
    find_window() 会优先读 `webview.windows[0].native.Handle`。WinForms 的
    `Control.Handle` 只有在**创建它的线程**上读才是直接返回，在别的线程读会被
    marshal 到 GUI 线程并**阻塞等待**。而我们这个加固线程要用的时刻，GUI 线程
    恰好正忙于初始化 WebView2 —— 一旦初始化变慢（多个实例抢同一个
    user data folder 时会明显变慢），这个线程就永久挂起：
    加固不会执行、超时日志也写不出来，app.log 停在「窗口模式启动」之后再无输出
    （2026-09-29 实测复现过：窗口卡在 300x300 不可见）。

    枚举和取属性用的都是 user32 的只读接口，对同进程窗口不会触发跨线程消息，
    所以这条路是安全的。

    :param visible_only: 窗口改成「先隐藏、等页面画好再显示」之后，加固要在显示
        之前完成，那时窗口还不可见 —— 需要能带上不可见的那一批。
    """
    cands = _own_windows(visible_only=visible_only)
    if not cands:
        return None
    for h, t, _a in cands:
        if t and (title in t or t in title):
            return h
    return max(cands, key=lambda c: c[2])[0]


def harden_window(hwnd: int) -> bool:
    """把窗口变成「零非客户区 + 原生四边缩放 + Win11 圆角」。"""
    if not _IS_WIN or not hwnd:
        return False

    style = int(_user32.GetWindowLongPtrW(hwnd, GWL_STYLE))
    new_style = style | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
    _user32.SetWindowLongPtrW(hwnd, GWL_STYLE, new_style)

    orig = int(_user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC))
    proc = WNDPROC(_wnd_proc)
    _HOOKS.append(proc)                         # 保活
    _STATE.update(hwnd=hwnd, orig=orig, proc=proc, hooked=True)
    _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC,
                              ctypes.cast(proc, ctypes.c_void_p).value)

    _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)

    # Win11 圆角 + 去掉残留的 1px 描边
    try:
        corner = ctypes.c_int(DWMWCP_ROUND)
        _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                      ctypes.byref(corner), ctypes.sizeof(corner))
        border = ctypes.c_uint(DWMWA_COLOR_NONE)
        _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR,
                                      ctypes.byref(border), ctypes.sizeof(border))
    except Exception:                           # noqa: BLE001
        pass

    # 有些启动方式会让窗口一出生就是最小化：Windows 规定「进程的首次 ShowWindow
    # 采用 STARTUPINFO.wShowWindow」，被别的程序拉起、或被脚本/批处理启动时，
    # 那个值可能是 SW_SHOWMINNOACTIVE —— 用户双击没反应，界面缩在任务栏里。
    # 加固正好在启动后几秒内执行，这时用户不可能已经自己点过最小化，所以看到
    # 图标化就还原一次，是安全的。（实测：从后台任务启动确实会命中这个状态）
    try:
        if _user32.IsIconic(hwnd):
            console.log_event("窗口启动时处于最小化，已自动还原")
            _user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:                           # noqa: BLE001
        pass

    console.log_event(f"窗口加固完成：hwnd={hwnd:#x}（无边框 + 原生缩放）")
    return True


def _default_size() -> Tuple[int, int]:
    """默认尺寸，并夹进当前屏幕的工作区。"""
    sw, sh = 1280, 800
    if _IS_WIN:
        r = wintypes.RECT()
        if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            sw, sh = r.right - r.left, r.bottom - r.top
    w = min(DEFAULT_W, max(MIN_W, sw - 120))
    h = min(DEFAULT_H, max(MIN_H, sh - 120))
    return int(w), int(h)


def _boot_bg() -> str:
    """页面首帧之前的窗口底色，跟系统浅/深色主题对齐，避免白屏闪一下。"""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            light, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return "#F5F4EE" if light else "#1F1E1B"
    except Exception:                           # noqa: BLE001
        return "#1F1E1B"


# ---------------------------------------------------------------------------
# 窗口操作（供 Flask 端点在别的线程里调用）
# ---------------------------------------------------------------------------
# 为什么不用 pywebview 自带的 js_api：
#   pywebview 6 把 js_api 挂在**它自己的内置 HTTP 服务**上，而 `webview.start()`
#   的 `http_server` 默认为 False；我们又加载的是外部 Flask 地址（不是本地文件），
#   `has_local_urls` 也不成立 —— 结果 `Window.js_api_endpoint` 恒为 None，
#   JS 侧 `window.pywebview.api` 是个**空对象**，点了毫无反应（已实测）。
#   既然 Flask 本来就在跑，窗口控制直接走我们自己的接口最省事，也少一层依赖。
def minimize_window() -> bool:
    w = _STATE.get("window")
    if w is None:
        return False
    try:
        w.minimize()            # winforms 后端内部走 Invoke，跨线程安全
        return True
    except Exception as e:      # noqa: BLE001
        console.log_event(f"最小化窗口失败：{type(e).__name__}: {e}")
        return False


def close_window() -> bool:
    w = _STATE.get("window")
    if w is None:
        return False
    try:
        w.destroy()             # 同上，destroy_window 也是 Invoke 到 UI 线程
        return True
    except Exception as e:      # noqa: BLE001
        console.log_event(f"关闭窗口失败：{type(e).__name__}: {e}")
        return False


def is_maximized() -> bool:
    """窗口当前是否最大化。

    用 Win32 的 `IsZoomed` + 样式位读，而不是 pywebview 的 `WindowState`：
    纯读操作，从任意线程调都安全，也不会被 winforms 内部状态滞后影响。
    """
    if not _IS_WIN:
        return False
    hwnd = _STATE.get("hwnd")
    if not hwnd:
        return False
    return _is_zoomed(hwnd)


def toggle_maximize_window() -> Optional[bool]:
    """最大化 / 还原互换。

    :return: True  = 切换后处于最大化
             False = 切换后处于还原态
             None  = 没拿到窗口，切换没发生（前端保持原状）
    """
    w = _STATE.get("window")
    if w is None:
        return None

    zoomed = is_maximized()
    try:
        if zoomed:
            w.restore()         # winforms: WindowState = Normal，内部 Invoke
        else:
            w.maximize()        # winforms: WindowState = Maximized，内部 Invoke
        state = not zoomed
        console.log_event(f"窗口{'最大化' if state else '还原'}"
                          f"（hwnd={int(_STATE['hwnd'] or 0):#x}）")
        return state
    except Exception as e:      # noqa: BLE001
        console.log_event(f"切换最大化失败：{type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
_webview_mod: Any = None
_webview_failed = False


def _load_webview() -> Any:
    """导入并**缓存** pywebview。

    为什么要缓存：这个 import 在 onefile 冷启动时不是免费的 —— 它要拉起
    pythonnet/CLR，实测是启动关键路径上最先花掉的一笔。`is_supported()` 会在
    决定用哪种承载方式时先导入一次，`run_window()` 紧接着又要用；不缓存就是
    同一个昂贵的导入做两遍。
    """
    global _webview_mod, _webview_failed
    if _webview_mod is not None:
        return _webview_mod
    if _webview_failed:
        return None
    try:
        import webview
        _webview_mod = webview
        return webview
    except Exception:                           # noqa: BLE001
        _webview_failed = True
        return None


def focus_window() -> bool:
    """把窗口从最小化还原并置前（重复双击 exe 时用）。"""
    if not _IS_WIN or not _STATE["hwnd"]:
        return False
    hwnd = _STATE["hwnd"]
    try:
        _user32.ShowWindow(hwnd, SW_RESTORE)
        _user32.SetForegroundWindow(hwnd)
        return True
    except Exception:                           # noqa: BLE001
        return False


def is_supported() -> bool:
    """当前环境能否用「自己的窗口」承载界面。

    要求：Windows + pywebview 可导入。真正的 WebView2 运行时缺失会在
    `run_window()` 里暴露，调用方那时再退回浏览器。

    用 `find_spec` 而不是真 import —— 探测本身要快，而且结果会被 `_load_webview`
    缓存复用，真正那次昂贵导入只发生在 run_window 里。
    """
    if not _IS_WIN:
        return False
    if _webview_mod is not None:
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("webview") is not None
    except Exception:                           # noqa: BLE001
        return False


def run_window(url: str, title: str,
               fallback_log: bool = True,
               on_ready: Optional[Callable[[Any], None]] = None) -> bool:
    """开一个无边框窗口装载 url。窗口关闭后返回。

    :param fallback_log: 环境不支持时是否写日志
    :param on_ready: 仅供自动化验证使用。窗口起来并加固后调用，回调收到
        pywebview 的 window 对象（可用 `evaluate_js` 检查页面）。生产代码不传。
    :return: True  = 窗口正常跑完并关闭
             False = 环境不支持（缺 WebView2 等），调用方应退回浏览器
    """
    global _focus_cb

    if not _IS_WIN:
        return False

    webview = _load_webview()
    if webview is None:
        if fallback_log:
            console.log_event("无法加载窗口组件，退回浏览器（pywebview 导入失败）")
        return False

    # 拖动区：给元素加 .pywebview-drag-region 即可拖动窗口，其它区域事件不受影响。
    # 不做整窗拖动（easy_drag=False），否则页面里选字、拖滚动条都会变成拖窗口。
    webview.settings["DRAG_REGION_SELECTOR"] = ".pywebview-drag-region"
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = False

    w, h = _default_size()
    try:
        # ★ hidden=True：窗口先创建但不显示，等页面首帧画好（或超时兜底）再显示。
        #
        #   为什么不是「一开始就可见」（10-03 实测过，无效）：
        #   Chromium **对不可见窗口挂起渲染**，反过来也一样 —— 窗口虽然可见了，
        #   但 WebView2 在「从隐藏变可见」的那一刻还没合成过任何一帧，所以页面里
        #   那个纯静态的 #bootMask 遮罩**照样画不出来**。连拍实测（打包态）：
        #   窗口 0.65s 就显示，屏幕上 0.65~6.7s 全是空底色（颜色数 7~25），
        #   直到 6.88s 真实界面才一次性出现。`w.refresh()` 催重绘也无效。
        #
        #   ⇒ 「提前显示窗口」在用户体感上没有收益，两条路都是等 ~4.3s。
        #     所以保留 hidden=True：它至少能保证「窗口出现时内容已在里面」，
        #     用户看到的第一眼就是完整界面（首帧正常时 ~1.3s，见 9-30 日志）。
        #     真正的杠杆是把 WebView2 首帧本身做快，不是窗口可见性。
        window = webview.create_window(
            title, url,
            width=w, height=h, min_size=(MIN_W, MIN_H),
            frameless=True,
            easy_drag=False,
            shadow=True,
            text_select=True,
            background_color=_boot_bg(),
            hidden=True,
        )
    except Exception as e:                      # noqa: BLE001
        if fallback_log:
            console.log_event(f"创建窗口失败，退回浏览器：{type(e).__name__}: {e}")
        return False

    _STATE["window"] = window                      # 供 minimize_window/close_window 用

    # 启动握手：记下「窗口已创建」的时刻，并起看门狗等前端的首帧信号。
    # 这一刻就是用户看到窗口（一片纯色）的时刻，拿它算「窗口→首帧」的耗时。
    with _BOOT_LOCK:
        _BOOT["t0"] = time.time()
        _BOOT["at"] = None
        _BOOT["payload"] = None
        _BOOT["reloads"] = 0
        _BOOT["revealed"] = False
    threading.Thread(target=_boot_watchdog, args=(window, url),
                     daemon=True).start()

    def _attach() -> None:
        # ★ 用 find_window_safe（纯 Win32）而不是 find_window，原因见它的注释：
        #   一旦这个线程因为跨线程读 WinForms 句柄而挂起，窗口就再也加固不了，
        #   日志也停在半路，事后完全没法排查。
        try:
            deadline = time.time() + _ATTACH_TIMEOUT
            while time.time() < deadline:
                # visible_only=False：窗口现在是隐藏创建的，加固要在它显示之前
                # 就做完，这样它第一次出现在屏幕上时样式就是对的（不会先看到
                # 一个带边框的窗口再「变」成无边框）。
                hwnd = find_window_safe(title, visible_only=False)
                if hwnd:
                    with _BOOT_LOCK:
                        t0 = _BOOT["t0"]
                    if t0:
                        console.log_event(
                            f"窗口已创建：创建后 {time.time() - t0:.2f}s")
                    try:
                        harden_window(hwnd)
                    except Exception as e:      # noqa: BLE001
                        console.log_event(f"窗口加固失败（不影响使用）：{e}")
                    return
                time.sleep(0.2)
            console.log_event(
                f"等窗口句柄超时（{_ATTACH_TIMEOUT:.0f}s）—— 窗口一直没创建出来，"
                f"多半是 WebView2 初始化卡住了；本次没有加固")
        except Exception as e:                  # noqa: BLE001
            console.log_event(f"窗口加固线程异常：{type(e).__name__}: {e}")

    threading.Thread(target=_attach, daemon=True).start()

    _focus_cb = focus_window

    console.log_event(f"窗口模式启动：{url}（{w}x{h}，无边框）")

    # ★ 必须换成持久 profile，别用 pywebview 默认的 private_mode=True。
    #   私有模式给 WebView2 一个**临时** user-data 目录，关窗时整个删掉 ——
    #   本机上这一步要十几秒（安全组件逐个检查大量小文件，与 onefile 清 _MEI 同源）。
    #   实测（本机）：private_mode=True → destroy() 阻塞 11.9~12.5s，
    #                 窗口点了关闭会在屏幕上卡着不消失；
    #                 持久 profile → 0.03~0.04s（快约 300 倍）。
    #   顺带的好处：localStorage 会保留，前端记的主题选择不再每次重启就丢。
    start_kw: Dict[str, Any] = {
        "debug": bool(os.environ.get("DYD_WEBVIEW_DEBUG"))}
    try:
        from . import config as cfg_mod
        profile_dir = cfg_mod.get_data_dir() / "webview_profile"
        os.makedirs(profile_dir, exist_ok=True)
        start_kw.update(private_mode=False, storage_path=str(profile_dir))
    except Exception as e:                      # noqa: BLE001
        # 拿不到目录只是关窗慢十几秒，不该因此让窗口起不来
        console.log_event(f"WebView 持久 profile 不可用（关窗会慢十几秒）：{e}")

    if on_ready is not None:
        def _ready() -> None:
            time.sleep(2.5)                     # 等加固线程跑完，免得量到中间态
            try:
                on_ready(window)
            except Exception as e:              # noqa: BLE001
                console.log_event(f"on_ready 回调异常：{type(e).__name__}: {e}")
        start_kw["func"] = _ready

    webview.start(**start_kw)
    return True


def focus_callback() -> Optional[Callable[[], None]]:
    return _focus_cb
