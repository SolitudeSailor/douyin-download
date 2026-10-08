# -*- coding: utf-8 -*-
"""验证「窗口亮出来的那一刻，用户看到的是界面还是空白」。

窗口现在是隐藏创建、等页面首帧就绪才显示的（见 win_window.reveal_window）。
但隐藏窗口里 Chromium 会暂停渲染，`show()` 之后还要重新合成一次 —— 这几百
毫秒里窗口可能只有一层 background_color。本脚本连拍窗口出现后的前 1.5 秒，
把这段「重新合成的时间」量出来。

★ 抓图前必须把窗口抢到前台，否则 BitBlt 抓到的是遮挡它的别的窗口
  （本机踩过：抓到编辑器的 #141414 界面冒充我们的）。

用法：
    python docs\\probes\\verify_reveal.py
输出：控制台时间线 + %TEMP%\\dyd_v2\\reveal_*.png
"""
from __future__ import annotations

import ctypes
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

u = gb.u
EXE = ROOT / "dist" / "douyin-tool.exe"
OUT = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2"
BG = (0x1F, 0x1E, 0x1B)
BRAND = (0xD9, 0x77, 0x57)


def shot_stats(hwnd: int, tag: str) -> dict:
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
    r = gb.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    W, H = r.right - r.left, r.bottom - r.top
    if W <= 0 or H <= 0:
        return {"tag": tag, "err": "窗口尺寸无效"}
    gb.force_foreground(hwnd)
    rgb = gb.grab(r.left, r.top, W, H)
    px = list(zip(rgb[0::3], rgb[1::3], rgb[2::3]))
    cnt = Counter(px)
    top = cnt.most_common(3)
    brand = any(abs(c[0] - BRAND[0]) < 16 and abs(c[1] - BRAND[1]) < 16
                and abs(c[2] - BRAND[2]) < 16 for c in px)
    p = OUT / f"reveal_{tag}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    gb.write_png(p, W, H, rgb)
    return {"tag": tag, "colors": len(cnt), "brand": brand,
            "top": [f"#{c[0]:02X}{c[1]:02X}{c[2]:02X}:{n*100.0/len(px):.0f}%"
                    for c, n in top],
            "bgish": round(cnt.get(BG, 0) * 100.0 / len(px), 1),
            "shot": str(p)}


def main() -> int:
    print(f"启动 {EXE.name} …")
    t0 = time.time()
    p = subprocess.Popen([str(EXE)])
    print(f"  [{0:.2f}s] 已启动（引导 pid={p.pid}）")

    hwnd = 0
    while time.time() - t0 < 60:
        cands = gb_all(gb.APP_TITLE)
        if cands:
            hwnd = cands[0]
            break
        time.sleep(0.05)
    t_show = round(time.time() - t0, 2)
    print(f"  [{t_show}s] 窗口出现在屏幕上 hwnd={hwnd:#x}")
    if not hwnd:
        print("  × 窗口一直没出现")
        return 1

    print("\n窗口出现后连拍：")
    marks = [0.0, 0.15, 0.3, 0.5, 0.8, 1.2, 2.0]
    base = time.time()
    prev = 0.0
    for i, m in enumerate(marks):
        time.sleep(max(0.0, m - prev))
        prev = m
        st = shot_stats(hwnd, f"{i}_{int(m * 1000):04d}ms")
        print(f"  +{m*1000:5.0f}ms 颜色数={st.get('colors')} "
              f"品牌色={st.get('brand')} 底色占比={st.get('bgish')}% "
              f"主色={st.get('top')}")

    print("\n=== 判定 ===")
    print("  窗口一亮就有内容（颜色数上千 + 品牌色出现）= 用户不会再看到空白")
    print("  前面几帧只有底色 = show() 之后还需要重新合成，需要相应处理")
    return 0


def gb_all(title: str):
    """枚举所有可见的同名顶层窗口。"""
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND,
                              ctypes.c_ssize_t)
    found = []

    def cb(h, _):
        n = u.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            if buf.value == title and u.IsWindowVisible(h):
                found.append(int(h))
        return True

    u.EnumWindows(proc(cb), 0)
    return found


if __name__ == "__main__":
    sys.exit(main())
