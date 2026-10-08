# -*- coding: utf-8 -*-
"""冷启动耗时验证 —— 从「双击 exe」到「页面真的画出来」测各阶段耗时。

当出现「打开后窗口一片空白」「界面半天不出来」的反馈时，先跑这个，
把「主观感觉」变成「客观时间线」。

实测基线（1180x860 无边框窗口，本机）：
    2.4s  running.json 出现（HTTP 服务起来）
    2.5s  窗口句柄出现
    2.5s  检测到品牌色 → 页面渲染完成
    → 窗口出现时内容已经画好，**不存在明显空白期**

用法：
    python docs\\probes\\verify_coldstart.py
输出：控制台时间线 + %TEMP%\\dyd_v2\\coldstart_full.png

★ 测完不关闭进程，方便人工接着看。要关：点窗口右上角 X，
  或 POST /api/window/close（端口见数据目录下 running.json）。
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import grab_buttons as gb  # noqa: E402
from douyin_tool.config import get_data_dir  # noqa: E402

RUNNING = get_data_dir() / "running.json"
EXE = ROOT / "dist" / "douyin-tool.exe"
u = gb.u

BRAND = (0xD9, 0x77, 0x57)      # 界面的品牌强调色（logo / 主按钮）


def alive(port: int):
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(f"http://127.0.0.1:{port}/api/alive", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def probe(x: int, y: int, w: int, h: int) -> tuple[int, bool]:
    """返回（不同颜色数, 是否出现品牌色）。

    ★ 不要用「颜色数变多」当渲染完成的判据：窗口刚创建时就会铺一层背景色，
      那个判据会过早通过（实测 2.99s 就误判为「已渲染」，其实整窗 99.6% 是纯背景）。
      用**特征色**（品牌色 / 某个只有内容才有的颜色）才靠谱。
    """
    rgb = gb.grab(x, y, w, h)
    px = list(zip(rgb[0::3], rgb[1::3], rgb[2::3]))
    n = len(set(px))
    brand = any(abs(r - BRAND[0]) < 14 and abs(g - BRAND[1]) < 14 and abs(b - BRAND[2]) < 14
                for r, g, b in px)
    return n, brand


def main() -> int:
    if RUNNING.exists():          # 清掉上次可能残留的
        RUNNING.unlink()

    t0 = time.time()
    p = subprocess.Popen([str(EXE)])
    print(f"[{0:6.2f}s] 双击 exe（引导进程 pid={p.pid}）")

    port = None
    while time.time() - t0 < 90:
        if RUNNING.exists():
            try:
                port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
                break
            except Exception:
                pass
        time.sleep(0.15)
    print(f"[{time.time() - t0:6.2f}s] running.json 出现 → 端口 {port}")

    t_http = None
    while time.time() - t0 < 90:
        if alive(port):
            t_http = time.time() - t0
            break
        time.sleep(0.15)
    print(f"[{t_http:6.2f}s] HTTP /api/alive 就绪")

    hwnd = 0
    while time.time() - t0 < 90:
        hwnd = gb.find_hwnd(gb.APP_TITLE)
        if hwnd:
            break
        time.sleep(0.15)
    print(f"[{time.time() - t0:6.2f}s] 窗口句柄出现 hwnd={hwnd}")

    # ★ 必须先置前！否则 BitBlt 抓的是屏幕上遮挡该矩形的**别的窗口**，
    #   会把别人的深灰界面统计成「我们的页面已渲染」
    #   （实测踩过：抓到一个 #141414 的编辑器窗口，被误当成 #262521 的界面）
    while time.time() - t0 < 90:
        if gb.force_foreground(hwnd):
            break
        time.sleep(0.3)
    print(f"[{time.time() - t0:6.2f}s] 窗口置前成功（前台确认）")

    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
    r = gb.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, hgt = r.right - r.left, r.bottom - r.top
    print(f"           窗口尺寸 {w}x{hgt} @ ({r.left},{r.top})")

    t_bg = None
    t_render = None
    trace = []
    while time.time() - t0 < 60:
        if (u.GetForegroundWindow() or 0) != hwnd:      # 掉到后台就抢回来
            gb.force_foreground(hwnd)
            time.sleep(0.15)
        n, brand = probe(r.left, r.top, 500, 160)
        trace.append((round(time.time() - t0, 2), n, brand))
        if t_bg is None and n > 40:
            t_bg = time.time() - t0
        if brand:
            t_render = time.time() - t0
            break
        time.sleep(0.15)
    print(f"[{t_bg:6.2f}s] 窗口背景出现（颜色数 > 40）")
    print(f"[{t_render:6.2f}s] 页面渲染完成（检测到品牌色 #D97757）")
    print(f"           探测轨迹: {trace[:16]}")

    time.sleep(1.0)              # 让页面稳定，避免抓到动画中间态
    rgb = gb.grab(r.left, r.top, w, hgt)
    out = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2" / "coldstart_full.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    gb.write_png(out, w, hgt, rgb)
    cnt = Counter(zip(rgb[0::3], rgb[1::3], rgb[2::3]))
    print(f"\n整窗截图 → {out}  ({out.stat().st_size} 字节)")
    print(f"不同颜色数 = {len(cnt)}  （上千 = 有真实内容）")
    for (rr, gg, bb), c in cnt.most_common(4):
        print(f"   #{rr:02X}{gg:02X}{bb:02X}  {c * 100.0 / (w * hgt):5.1f}%")
    print(f"\n★ 从双击到界面可用：{t_render:.2f} 秒")
    print("   （进程保持运行，看完点窗口右上角 X 即可）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
