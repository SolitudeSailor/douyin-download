# -*- coding: utf-8 -*-
"""Cookie 文本工具 + 浏览器探测。

背景（实测结论，2026-09）
------------------------
Chrome 127+ 启用 **App-Bound Encryption**，Cookies 数据库里的值以 ``v20``
前缀加密，密钥由浏览器进程保护，**外部程序无法解密**（实测 76 条 douyin
cookie 全部解密失败）。所以「直读数据库」的老办法已彻底失效。

本项目的解决方案转向 **CDP（Chrome DevTools Protocol）**，实现放在
``src/douyin_tool/browser_login.py``；本模块只保留两件事：

1. **浏览器探测**：找到 Chrome / Edge 的可执行文件（CDP 方案要用）
2. **Cookie 文本工具**：解析与校验 cookie 串

注意：本模块不做任何 cookie 窃取式破解。CDP 方式本质是用户授权下用自己
的浏览器读自己的登录态，与「读取他人 cookie」有本质区别。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

# 各浏览器可执行文件候选路径（按优先级）
BROWSER_PATHS: Dict[str, List[str]] = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ],
}

# 自动探测顺序：Chrome 优先，Edge 兜底
BROWSER_ORDER = ("chrome", "edge")

BROWSER_LABEL = {"chrome": "Chrome", "edge": "Edge"}


def find_browser(name: str = "chrome") -> Optional[str]:
    """按名称定位浏览器可执行文件，找不到返回 None。"""
    for p in BROWSER_PATHS.get(name.lower(), []):
        if p and os.path.exists(p):
            return p
    return None


def detect_browser(prefer: Optional[str] = None):
    """自动探测可用的浏览器。

    参数 prefer 可指定 "chrome" / "edge" 优先；未指定则 Chrome 优先。
    返回 ``(名称, exe路径)`` 或 ``None``。
    """
    order = list(BROWSER_ORDER)
    if prefer:
        want = prefer.lower()
        if want in order:
            order.remove(want)
        order.insert(0, want)
    for name in order:
        exe = find_browser(name)
        if exe:
            return BROWSER_LABEL.get(name, name.title()), exe
    return None


def free_port() -> int:
    """找一个空闲的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_cdp(port: int, timeout: float = 25.0) -> Optional[str]:
    """等待 CDP 调试端口就绪，返回 websocket 调试地址（未就绪返回 None）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            ) as r:
                info = json.load(r)
            ws = info.get("webSocketDebuggerUrl")
            if ws:
                return ws
        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
            pass
        time.sleep(0.4)
    return None


def parse_cookie_string(raw: str) -> Dict[str, str]:
    """把 cookie 字符串解析成字典，顺带做基本清洗。"""
    result: Dict[str, str] = {}
    if not raw:
        return result
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    for seg in text.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            result[k] = v
    return result


def validate_cookie(raw: str) -> Tuple[bool, str]:
    """校验 cookie 串是否含登录态。返回 (是否可用, 说明)。"""
    if not raw or not raw.strip():
        return False, "Cookie 为空。"

    d = parse_cookie_string(raw)
    if not d:
        return False, "Cookie 格式不正确。"

    if not any(k in d for k in ("sessionid", "sessionid_ss")):
        return False, (
            "Cookie 缺少 sessionid，未包含登录态。"
            "请点「打开登录窗口」重新登录。"
        )

    if "ttwid" not in d:
        return True, "Cookie 缺少 ttwid，但可能仍可用（接口会自动补全）。"

    return True, f"Cookie 有效，共 {len(d)} 个字段。"
