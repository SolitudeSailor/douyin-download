# -*- coding: utf-8 -*-
"""抓「当前正在运行」的应用窗口截图并统计颜色分布 —— 排查白屏/空白的现场取证工具。

与 verify_coldstart.py 的区别：它自己启动 exe；本脚本**附着到已经开着的窗口**，
适合用户报「打开后一片空白」时，直接把那一刻的窗口画出来看。

用法：
    python docs\\probes\\diag_live.py            # 抓图到 %TEMP%\\dyd_v2\\live_now.png
    python docs\\probes\\diag_live.py 600 400    # 只抓窗口左上角一块（更快）
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import sys
import time
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import grab_buttons as gb  # noqa: E402
from douyin_tool.config import get_data_dir  # noqa: E402

u = gb.u


def main() -> int:
    hwnd = gb.find_hwnd(gb.APP_TITLE)
    print(f"窗口句柄 hwnd={hwnd}")
    if not hwnd:
        print("× 没找到运行中的窗口，先双击 exe 或跑 verify_coldstart.py")
        return 1

    ok = gb.force_foreground(hwnd)
    print(f"置前 {'成功' if ok else '失败'}")
    fol = u.GetForegroundWindow() or 0
    print(f"前台窗口 = {fol}（应等于 {hwnd}）")

    try:
        port = json.loads((get_data_dir() / "running.json").read_text(encoding="utf-8"))["port"]
        print(f"HTTP 端口 = {port}  /api/alive = {gb.http('GET', f'http://127.0.0.1:{port}/api/alive')}")
    except Exception as e:  # noqa: BLE001
        print(f"读 running.json 失败：{e}")

    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
    r = gb.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    print(f"窗口矩形 ({r.left},{r.top}) {w}x{h}")

    time.sleep(0.6)
    rgb = gb.grab(r.left, r.top, w, h)
    out = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2" / "live_now.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    gb.write_png(out, w, h, rgb)
    cnt = Counter(zip(rgb[0::3], rgb[1::3], rgb[2::3]))
    print(f"\n截图 → {out}  ({out.stat().st_size} 字节)")
    print(f"不同颜色数 = {len(cnt)}")
    for (rr, gg, bb), c in cnt.most_common(6):
        print(f"   #{rr:02X}{gg:02X}{bb:02X}  {c * 100.0 / (w * h):5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
