# -*- coding: utf-8 -*-
"""连续多次冷启动，测 WebView2 首帧的**波动**。

2026-10-03 排查用：同样的代码、同样的 profile，独立测到过 167ms 和 3367ms
两个相差 20 倍的首帧值。这个脚本连跑 N 次，把「波动」本身量化出来 ——
如果分布是双峰的，说明另有变量（机器负载 / WebView2 的某个一次性初始化/
安全软件扫描），不是代码问题。

用法：
    python docs\\probes\\verify_firstframe_variance.py [次数]
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from douyin_tool.config import get_data_dir  # noqa: E402

LOG = get_data_dir() / "logs" / "app.log"
RUNNING = get_data_dir() / "running.json"
PY = str(pathlib.Path(sys.executable))
FRAME_RE = re.compile(r"WebView2 首帧 (\d+)ms")


def one_trial(idx: int) -> dict:
    before = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    if RUNNING.exists():
        RUNNING.unlink()

    t0 = time.time()
    p = subprocess.Popen([PY, "-m", "douyin_tool"], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等日志里出现新的「页面已渲染」
    frame = dom = t_render = None
    while time.time() - t0 < 60:
        txt = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
        new = txt[len(before):]
        m = FRAME_RE.search(new)
        if m and "页面已渲染" in new:
            frame = int(m.group(1))
            t_render = time.time() - t0
            mm = re.search(r"界面就绪 (\d+)ms", new)
            dom = int(mm.group(1)) if mm else None
            break
        time.sleep(0.1)

    # 关闭
    try:
        port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open(f"http://127.0.0.1:{port}/api/window/close", data=b"", timeout=5)
    except Exception:                        # noqa: BLE001
        pass
    try:
        p.wait(timeout=25)
    except Exception:                        # noqa: BLE001
        p.kill()
    time.sleep(1.5)

    print(f"  第 {idx} 次：首帧 {frame}ms / 界面就绪 {dom}ms / 页面完成 {t_render:.2f}s")
    return {"frame": frame, "dom": dom, "render": t_render}


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(f"连跑 {n} 次冷启动（源码态）…\n")
    rows = [one_trial(i) for i in range(1, n + 1)]

    frames = [r["frame"] for r in rows if r["frame"] is not None]
    print("\n★ 首帧分布：", frames)
    if frames:
        print(f"   最小 {min(frames)}ms / 最大 {max(frames)}ms / "
              f"均值 {sum(frames) // len(frames)}ms")
        fast = [f for f in frames if f < 800]
        slow = [f for f in frames if f >= 800]
        print(f"   快(<800ms) {len(fast)} 次 / 慢(>=800ms) {len(slow)} 次")
        if fast and slow:
            print("   → 双峰分布：与代码无关，另有机器侧变量在干扰")
        elif fast:
            print("   → 全程都快，稳定")
        else:
            print("   → 全程都慢，需要继续定位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
