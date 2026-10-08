# -*- coding: utf-8 -*-
"""抓「窗口刚亮出那几秒」的真实画面 —— 证明用户看到的是转圈遮罩，不是空白。

2026-10-03 改动的核心主张：窗口不再「藏到页面画好」，而是 0.6s 就亮出来，
由页面里的纯静态 #bootMask 遮罩接管过渡。所以**窗口一亮，屏幕上就该有内容**
（转圈 + 文案），而不是一片纯底色。

判据：亮出后**立刻**抓图，统计不同颜色数。
    - 空白期：颜色数 ≈ 个位数（纯 #1F1E1B / #262521 底色）
    - 有遮罩：颜色数明显更多（转圈 + 文字的抗锯齿边缘）

用法：
    python docs\\probes\\verify_mask_visible.py
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
OUT = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2" / "mask_probe.png"


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
        time.sleep(0.03)

    t_vis = time.time() - t0
    print(f"[{t_vis:6.2f}s] 窗口可见 —— 立刻抓图")

    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
    r = gb.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, hgt = r.right - r.left, r.bottom - r.top

    gb.force_foreground(hwnd)
    time.sleep(0.05)
    rgb = gb.grab(r.left, r.top, w, hgt)
    px = list(zip(rgb[0::3], rgb[1::3], rgb[2::3]))
    n_immediate = len(set(px))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    gb.write_png(OUT, w, hgt, rgb)
    cnt = Counter(px)
    top = cnt.most_common(3)

    print(f"           窗口 {w}x{hgt}")
    print(f"           ★ 亮出瞬间的不同颜色数 = {n_immediate}")
    for (rr, gg, bb), c in top:
        print(f"             #{rr:02X}{gg:02X}{bb:02X}  {c * 100.0 / (w * hgt):5.1f}%")
    print(f"           截图 → {OUT}")

    print("\n★ 结论：")
    if n_immediate > 60:
        print(f"   ✅ 亮出瞬间颜色数 {n_immediate} > 60 —— 屏幕上不是纯底色，")
        print("      说明遮罩（转圈/文字）已经在位，用户不会看到空白")
        ok = True
    else:
        print(f"   ❌ 亮出瞬间只有 {n_immediate} 种颜色 —— 还是纯底色，遮罩没起作用")
        ok = False

    try:
        port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open(f"http://127.0.0.1:{port}/api/window/close", data=b"", timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
