# -*- coding: utf-8 -*-
"""抓「真实窗口右上角按钮区」的截图 —— 眼见为实。

前置：dist\\douyin-tool.exe 已在窗口模式运行（端口从数据目录的 running.json 读）。
用法：
    python docs\\probes\\grab_buttons.py          # 常态
    python docs\\probes\\grab_buttons.py max      # 最大化
输出：%TEMP%\\dyd_v2\\winbtn_<mode>.png
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import struct
import sys
import time
import urllib.request
import zlib
from ctypes import wintypes

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from douyin_tool.config import get_data_dir           # noqa: E402

# running.json 的落点由 src/douyin_tool/config.py 统一决定（= 数据目录），不要硬编码 dist/ ——
# 打包好的 exe 旁边若存在 data_dir.txt，数据会落到别处，硬编码就找不到端口了。
RUNNING = get_data_dir() / "running.json"
APP_TITLE = "抖音视频下载器"

u = ctypes.windll.user32
gdi = ctypes.windll.gdi32
k32 = ctypes.windll.kernel32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def write_png(path: pathlib.Path, w: int, h: int, rgb: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    blob = b"\x89PNG\r\n\x1a\n"
    blob += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    blob += chunk(b"IDAT", zlib.compress(rows, 9))
    blob += chunk(b"IEND", b"")
    path.write_bytes(blob)


def grab(x: int, y: int, w: int, h: int) -> bytes:
    # 句柄都是 64 位指针，必须显式声明 argtypes/restype，否则 ctypes 按 c_int 处理会溢出
    u.GetDC.restype = ctypes.c_void_p
    u.GetDC.argtypes = [ctypes.c_void_p]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi.SelectObject.restype = ctypes.c_void_p
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                           ctypes.c_uint]
    gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]

    hdc = u.GetDC(None)
    mem = gdi.CreateCompatibleDC(hdc)
    bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
    old = gdi.SelectObject(mem, bmp)
    gdi.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)      # SRCCOPY
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h                              # 负数 = top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi.GetDIBits(mem, bmp, 0, h, ctypes.cast(buf, ctypes.c_void_p),
                  ctypes.byref(bi), 0)
    gdi.SelectObject(mem, old)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    u.ReleaseDC(None, hdc)
    d = buf.raw
    out = bytearray(w * h * 3)
    out[0::3] = d[2::4]      # R
    out[1::3] = d[1::4]      # G
    out[2::3] = d[0::4]      # B
    return bytes(out)


def find_hwnd(title: str) -> int:
    u.FindWindowW.restype = ctypes.c_void_p
    return int(u.FindWindowW(None, title) or 0)


def force_foreground(hwnd: int) -> bool:
    u.GetForegroundWindow.restype = ctypes.c_void_p
    if (u.GetForegroundWindow() or 0) == hwnd:
        return True
    cur = k32.GetCurrentThreadId()
    tids = {u.GetWindowThreadProcessId(u.GetForegroundWindow() or 0, None),
            u.GetWindowThreadProcessId(hwnd, None)}
    attached = []
    for tid in tids:
        if tid and tid != cur and u.AttachThreadInput(cur, tid, True):
            attached.append(tid)
    try:
        u.ShowWindow(hwnd, 9)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            u.AttachThreadInput(cur, tid, False)
    for _ in range(12):
        if (u.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.15)
    return False


def http(method: str, url: str, timeout: int = 8):
    req = urllib.request.Request(url, method=method)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    port = int(json.loads(RUNNING.read_text(encoding="utf-8"))["port"])
    base = f"http://127.0.0.1:{port}"
    want_max = (mode == "max")

    cur = http("GET", base + "/api/window/maximize").get("maximized")
    if bool(cur) != want_max:
        http("POST", base + "/api/window/maximize")
        time.sleep(0.9)
        cur = http("GET", base + "/api/window/maximize").get("maximized")
    print(f"最大化状态 = {cur}（期望 {want_max}）")

    hwnd = find_hwnd(APP_TITLE)
    if not hwnd:
        print("x 找不到窗口")
        return 2
    print(f"置前 = {force_foreground(hwnd)}")
    time.sleep(0.5)

    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    r = RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    print(f"窗口 rect = ({r.left},{r.top})-({r.right},{r.bottom})  "
          f"{r.right - r.left}x{r.bottom - r.top}")

    # 裁切宽度跟着窗口走：窗口按钮在「居中的内容容器」里，不是贴着窗口右边缘 ——
    # 窗口一宽，按钮组离右边缘就越远，所以不能写死 340
    cw = min(700, max(340, int((r.right - r.left) * 0.45)))
    ch = 96
    x0 = max(r.left, r.right - cw)
    y0 = r.top
    rgb = grab(x0, y0, cw, ch)
    out = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2" / f"winbtn_{mode}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_png(out, cw, ch, rgb)
    print(f"已保存 {out}  ({out.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
