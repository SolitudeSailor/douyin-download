# -*- coding: utf-8 -*-
"""交替律验证 —— 连测 6 次、每次间隔固定，看首帧是否严格「一快一慢」交替。

2026-10-03 的日志里出现了一个极强的规律（见 app.log 18:27~18:31）：
    166ms(快) → 2335ms(慢) → 169ms(快) → 3329ms(慢) → 182ms(快)
    → 3334ms(慢) → 167ms(快) → 173ms(快) → 3393ms(慢) → 3273ms(慢)...
快档总是紧跟在一次启动之后。怀疑机制：**WebView2 的某个组件在两次启动之间
被「预热/驻留」，下一次启动就能秒开；而驻留会被下一次启动自己消耗掉**，
于是呈现交替。

本脚本固定间隔连测，把「第 N 次 / 首帧 / 距上次启动的间隔」三列打出来，
交替律成立的话一眼就能看出来。

用法：
    python docs\\probes\\verify_alternation.py [次数] [间隔秒]
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
# 没有首帧字段时（触发=load 的老路径），退而抓「窗口创建后 N.NNs」
FALLBACK_RE = re.compile(r"页面已渲染：窗口创建后 ([\d.]+)s")


def run_once() -> tuple[int | None, float]:
    before = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    if RUNNING.exists():
        RUNNING.unlink()

    t0 = time.time()
    p = subprocess.Popen([PY, "-m", "douyin_tool"], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frame = None
    while time.time() - t0 < 60:
        txt = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
        seg = txt[len(before):]
        m = FRAME_RE.search(seg)
        if m:
            frame = int(m.group(1))
            break
        # 兼容没有首帧字段的路径：用「窗口创建后 N.NNs」× 1000 当近似值
        f = FALLBACK_RE.search(seg)
        if f:
            frame = int(float(f.group(1)) * 1000)
            break
        time.sleep(0.05)

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
    return frame, time.time() - t0


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

    print(f"连测 {n} 次，每次间隔 {gap}s（源码态）\n")
    print("   次  首帧(ms)  档位   距上次启动(s)")
    rows = []
    last_end = None
    for i in range(1, n + 1):
        if last_end is not None:
            time.sleep(gap)
        frame, dur = run_once()
        last_end = time.time()
        tier = "快" if (frame or 9999) < 800 else "慢"
        gap_s = f"{dur:.1f}" if last_end else "-"
        print(f"   {i:2d}  {str(frame):>8}   {tier}    {gap_s}")
        rows.append((i, frame, tier))

    print("\n★ 档位序列：", " ".join(t for _, _, t in rows))
    tiers = [t for _, _, t in rows]
    if len(tiers) >= 4 and all(tiers[i] != tiers[i + 1] for i in range(len(tiers) - 1)):
        print("   → ✅ 严格交替！说明存在「一次启动消耗掉预热状态」的机制")
    elif tiers.count("快") and tiers.count("慢"):
        print("   → 快慢都有，但不是严格交替")
    else:
        print("   → 单一档位（本次没复现双峰）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
