# -*- coding: utf-8 -*-
"""前台/后台对首帧的影响 —— 验证「窗口被反复抢到前台/被屏幕遮挡」是否会拖慢渲染。

线索（2026-10-03）：
    9-30 首帧 1.0~1.8s（触发=load）；
    10-03 18:19 仍 1.66s；18:22 之后突变 4.3s（触发=raf）。
    18:22 正是第一次跑 verify_coldstart.py 的时刻 —— 那个脚本会
    force_foreground() + 高频 BitBlt 抓图。

假设：**窗口不可见/被遮挡时，Chromium 会降低渲染优先级**（这是真实存在的机制，
    为省电）。隐藏窗口 → 一直不画（已坐实）；反复被抢前台 + 抓图 → 反复打断。

本脚本对比三种情况下的首帧：
  A) 隐藏创建（hidden=True，当前生产行为）
  B) 可见创建（hidden=False），但**不碰它**（不抢前台、不抓图）
  C) 可见创建 + 像 coldstart 那样持续抢前台+BitBlt

用法：
    python docs\\probes\\verify_visibility_effect.py
"""
from __future__ import annotations

import ctypes
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import grab_buttons as gb  # noqa: E402
from douyin_tool.config import get_data_dir  # noqa: E402

u = gb.u
RUNNING = get_data_dir() / "running.json"
LOG = get_data_dir() / "logs" / "app.log"
# 必须用 pythonw.exe：python.exe 跑 pywebview 时会带一个控制台窗口，
# 而控制台窗口会**抢走前台**，污染 C 组的「抢前台」对照。
PY = str(pathlib.Path(sys.executable).with_name("pythonw.exe"))
assert pathlib.Path(PY).exists(), f"找不到 pythonw.exe: {PY}"

CODE = r'''
import sys, time, json
sys.path.insert(0, r"{root}\\src")
from douyin_tool import config as cfg_mod
import webview
prof = cfg_mod.get_data_dir() / "webview_profile"
webview.settings["DRAG_REGION_SELECTOR"]=".pywebview-drag-region"
w = webview.create_window("VIS", "http://127.0.0.1:{port}/",
                          width=1000, height=700, hidden={hidden})
webview.start(private_mode=False, storage_path=str(prof))
'''


def wait_boot(before: str, timeout: float = 40.0) -> float | None:
    t = time.time()
    while time.time() - t < timeout:
        seg = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
        if "页面已渲染" in seg[len(before):]:
            return time.time() - t
        time.sleep(0.05)
    return None


def main() -> int:
    print("=== 先起一个 Flask（源码态服务，供 A/B/C 复用）===")
    srv = subprocess.Popen([PY, "-m", "douyin_tool", "--no-browser"], cwd=str(ROOT),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = None
    for _ in range(120):
        try:
            port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
            break
        except Exception:
            time.sleep(0.25)
    print(f"服务端口 {port}")

    for label, hidden, interfere in [
        ("A hidden=True  (生产行为)", True, False),
        ("B hidden=False 不干扰", False, False),
        ("C hidden=False + 持续抢前台抓图", False, True),
    ]:
        before = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
        # 独立进程起一个窗口（不经过 douyin_tool，避免单实例守卫）
        proc = subprocess.Popen(
            [PY, "-c", CODE.format(root=str(ROOT), port=port, hidden=hidden)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        hwnd_stop = time.time() + 25
        while interfere and time.time() < hwnd_stop:
            h = gb.find_hwnd("VIS")
            if h:
                gb.force_foreground(h)
                try:
                    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
                    r = gb.RECT()
                    u.GetWindowRect(h, ctypes.byref(r))
                    gb.grab(r.left, r.top, 400, 200)
                except Exception:
                    pass
            time.sleep(0.12)

        dt = wait_boot(before)
        print(f"  {label:38s} → {'%.2fs' % dt if dt else '超时'}")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        time.sleep(1.5)

    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
