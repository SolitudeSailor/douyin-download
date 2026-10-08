# -*- coding: utf-8 -*-
"""采样 WebView2 进程的 CPU 时间曲线 —— 判断那 3.9 秒是「在算」还是「在等」。

已知（10-03 坐实）：WebView2 进程 1.21s 就全起来了，但页面 5.10s 才渲染
（触发=load、无首帧字段那条路径）。中间 3.9s 到底在干嘛？

判据：
  - CPU 时间在涨  → 在**算**（解析/编译/布局/网络重试）
  - CPU 时间不动  → 在**等**（等锁、等超时、等某个外部资源）

用法：
    python docs\\probes\\verify_webview2_cpu_curve.py
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from douyin_tool.config import get_data_dir  # noqa: E402

RUNNING = get_data_dir() / "running.json"
LOG = get_data_dir() / "logs" / "app.log"
PY = str(pathlib.Path(sys.executable))


def cpu_times() -> dict[str, float]:
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-Process msedgewebview2 | "
        "ForEach-Object { \"$($_.Id),$($_.TotalProcessorTime.TotalSeconds)\" }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=20)
        d = {}
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if "," in line:
                a, b = line.split(",", 1)
                if a.isdigit():
                    try:
                        d[a] = float(b)
                    except ValueError:
                        pass
        return d
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    if RUNNING.exists():
        RUNNING.unlink()
    base = cpu_times()
    log_before = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""

    t0 = time.time()
    p = subprocess.Popen([PY, "-m", "douyin_tool"], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[{0:6.2f}s] 启动 pid={p.pid}")
    print("   时刻   WebView2总CPU(s)  本帧增量   备注")

    prev = dict(base)
    t_page = None
    while time.time() - t0 < 40:
        now = time.time() - t0
        cur = cpu_times()
        total = sum(cur.values()) - sum(base.values())
        delta = sum(cur.values()) - sum(prev.values())
        prev = cur
        seg = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
        done = "页面已渲染" in seg[len(log_before):]
        print(f"  {now:6.2f}   {total:10.2f}   {delta:+7.3f}   "
              f"{'★ 页面就绪' if done and t_page is None else ''}")
        if done and t_page is None:
            t_page = now
            break
        time.sleep(0.3)

    if t_page:
        print(f"\n页面就绪 {t_page:.2f}s —— 期间 WebView2 累计 CPU {total:.2f}s")
        print(f"→ CPU/墙钟 比 = {total / t_page:.2f}"
              f"（远小于 1 = 大部分时间在等；接近 1 = 在满速算）")

    try:
        port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open(f"http://127.0.0.1:{port}/api/window/close", data=b"", timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
