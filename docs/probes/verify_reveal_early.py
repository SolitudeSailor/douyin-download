# -*- coding: utf-8 -*-
"""验证「窗口提前显示」——从启动到窗口**可见**用了多久。

背景（2026-10-03）：
    原设计是「窗口隐藏创建，等页面首帧画好再显示」，兜底超时 18 秒。
    WebView2 首帧一旦变慢（profile 用久后从 1.3s 涨到 3.3s），窗口就一直是
    隐藏的 —— 用户那边就是「双击了，屏幕上什么都没有」。

    改成 REVEAL_DEADLINE=0.6s 后：窗口先出来（页面里的纯静态启动遮罩接管
    这段过渡），页面画好后再撤遮罩。

本脚本量三件事：
    1) 窗口句柄出现（隐藏创建）的时刻
    2) 窗口**变为可见**的时刻   ← 这次改动的核心指标
    3) 页面渲染完成的时刻（读后端日志）

用法（源码态，不需要打包）：
    python docs\\probes\\verify_reveal_early.py
"""
from __future__ import annotations

import ctypes
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import grab_buttons as gb  # noqa: E402
from douyin_tool.config import get_data_dir  # noqa: E402

u = gb.u
LOG = get_data_dir() / "logs" / "app.log"
RUNNING = get_data_dir() / "running.json"
PY = str(pathlib.Path(sys.executable))


def is_visible(hwnd: int) -> bool:
    return bool(u.IsWindowVisible(hwnd))


def main() -> int:
    if RUNNING.exists():
        RUNNING.unlink()

    log_before = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""

    t0 = time.time()
    p = subprocess.Popen([PY, "-m", "douyin_tool"], cwd=str(ROOT))
    print(f"[{0:6.2f}s] 启动源码版（pid={p.pid}）")

    hwnd = 0
    while time.time() - t0 < 60:
        hwnd = gb.find_hwnd(gb.APP_TITLE)
        if hwnd:
            break
        time.sleep(0.05)
    t_hwnd = time.time() - t0
    print(f"[{t_hwnd:6.2f}s] 窗口句柄出现 hwnd={hwnd}（此时可见={is_visible(hwnd)}）")

    t_vis = None
    while time.time() - t0 < 60:
        if is_visible(hwnd):
            t_vis = time.time() - t0
            break
        time.sleep(0.05)
    print(f"[{t_vis:6.2f}s] ★ 窗口变为可见")

    # 页面渲染完成：等日志里出现「页面已渲染」
    t_render = None
    while time.time() - t0 < 60:
        txt = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
        new = txt[len(log_before):]
        if "页面已渲染" in new:
            t_render = time.time() - t0
            for line in new.splitlines():
                if "页面已渲染" in line or "窗口已显示" in line:
                    print(f"            | {line.strip()}")
            break
        time.sleep(0.1)
    print(f"[{t_render:6.2f}s] 页面渲染完成")

    time.sleep(1.0)
    rgb = gb.grab(0, 0, 0, 0)  # 占位，避免 lint 报未用
    print("\n★ 结论：")
    print(f"   窗口可见     {t_vis:.2f}s   （改前 = 等首帧，实测 6.91s）")
    print(f"   页面画完     {t_render:.2f}s")
    print(f"   → 用户从「双击」到「看到转圈」只等 {t_vis:.2f}s")

    # 收尾：优雅关闭
    try:
        port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open(f"http://127.0.0.1:{port}/api/window/close", data=b"", timeout=5)
        print("\n已发送关闭请求")
    except Exception as e:  # noqa: BLE001
        print(f"\n关闭失败（手动关窗口即可）：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
