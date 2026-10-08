# -*- coding: utf-8 -*-
"""连拍「窗口出现后头 4 秒」—— 看清遮罩到底什么时候画出来。

2026-10-03：`verify_mask_visible.py` 只在「窗口可见」那一刻抓一张，
但它很可能抓在遮罩还没合成的瞬间（0.05s 太早）。本脚本改成**连拍**，
把每次抓图的不同颜色数按时间排出来，就能看清：
    - 是一直空底色（遮罩根本没画）
    - 还是空底色几百毫秒后遮罩出现（只是抓早了）

用法：
    python docs\\probes\\verify_mask_timeline.py
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import time
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import grab_buttons as gb  # noqa: E402
from douyin_tool.config import get_data_dir  # noqa: E402

u = gb.u
RUNNING = get_data_dir() / "running.json"
EXE = ROOT / "dist" / "douyin-tool.exe"
OUTDIR = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2"


def colorful(rgb) -> int:
    return len(set(zip(rgb[0::3], rgb[1::3], rgb[2::3])))


def main() -> int:
    if RUNNING.exists():
        RUNNING.unlink()

    t0 = time.time()
    p = subprocess.Popen([str(EXE)])
    print(f"[{0:6.2f}s] 双击 exe（pid={p.pid}）")

    hwnd = 0
    while time.time() - t0 < 60:
        hwnd = gb.find_hwnd(gb.APP_TITLE)
        if hwnd and u.IsWindowVisible(hwnd):
            break
        time.sleep(0.02)
    print(f"[{time.time() - t0:6.2f}s] 窗口可见，开始连拍")

    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
    r = gb.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, hgt = r.right - r.left, r.bottom - r.top

    rows = []
    OUTDIR.mkdir(parents=True, exist_ok=True)
    for i in range(28):                    # 约 4 秒
        if (u.GetForegroundWindow() or 0) != hwnd:
            gb.force_foreground(hwnd)
        rgb = gb.grab(r.left, r.top, 500, 160)     # 只抓左上角，够判遮罩
        n = colorful(rgb)
        rows.append((round(time.time() - t0, 2), n))
        if i in (0, 3, 8, 15):                     # 存几张关键帧
            full = gb.grab(r.left, r.top, w, hgt)
            gb.write_png(OUTDIR / f"mask_t{i:02d}.png", w, hgt, full)
        time.sleep(0.14)

    print("\n   时刻(s)  颜色数")
    for t, n in rows:
        bar = "#" * min(60, n // 4)
        print(f"   {t:6.2f}   {n:5d}  {bar}")

    first_colorful = next((t for t, n in rows if n > 60), None)
    print("\n★ 结论：")
    if first_colorful is None:
        print("   ❌ 4 秒内颜色数始终 < 60 —— 遮罩根本没画出来")
    else:
        print(f"   ✅ 窗口出现后 {first_colorful:.2f}s 颜色数 > 60（遮罩已可见）")
    print(f"   关键帧 → {OUTDIR}\\mask_t*.png")

    try:
        port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open(f"http://127.0.0.1:{port}/api/window/close", data=b"", timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
