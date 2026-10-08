# -*- coding: utf-8 -*-
"""直接测 WebView2 进程树的启动耗时 —— 找出 3.3s 到底花在哪一步。

之前所有测量都来自「应用自己打的点」，只能知道总时长。
本脚本改为**从外部观察 msedgewebview2.exe 进程的诞生与就绪**：
  1) 起应用
  2) 同时高频采样 msedgewebview2.exe 进程列表（pid + 创建时间 + CPU 时间）
  3) 看首个 WebView2 进程什么时候出现、什么时候有 CPU 活动
从而区分：
  A) WebView2 进程「很久才被拉起来」 → 卡在 pywebview/.NET 侧
  B) 进程很快就起来，但「一直不干活」 → 卡在 WebView2 内部初始化

用法：
    python docs\\probes\\verify_webview2_pid_timeline.py
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


def wv_procs() -> dict[int, float]:
    """返回 {pid: CPU秒数}。用 PowerShell CIM 拿创建时间与 CPU。"""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId),$($_.CreationDate.ToString('o'))\" }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
        out = r.stdout.decode("utf-8", "replace")
        d = {}
        for line in out.splitlines():
            line = line.strip()
            if "," in line and line.split(",")[0].isdigit():
                pid, ts = line.split(",", 1)
                d[int(pid)] = ts
        return d
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    if RUNNING.exists():
        RUNNING.unlink()
    before = set(wv_procs())

    log_before = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""

    t0 = time.time()
    p = subprocess.Popen([PY, "-m", "douyin_tool"], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[{0:6.2f}s] 启动应用 pid={p.pid}")

    seen: dict[int, float] = {}
    t_page = None
    while time.time() - t0 < 60:
        now = time.time() - t0
        cur = wv_procs()
        for pid, ts in cur.items():
            if pid not in before and pid not in seen:
                seen[pid] = now
                print(f"[{now:6.2f}s] ★ 新 WebView2 进程 pid={pid}")
        seg = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
        if t_page is None and "页面已渲染" in seg[len(log_before):]:
            t_page = now
            print(f"[{now:6.2f}s] 页面已渲染")
            break
        time.sleep(0.12)

    print(f"\n共观察到 {len(seen)} 个新 WebView2 进程")
    if seen:
        first = min(seen.values())
        print(f"首个 WebView2 进程出现在 {first:.2f}s，页面就绪在 {t_page:.2f}s")
        print(f"→ 「拉起 WebView2 进程」占 {first:.2f}s，"
              f"「进程起来后到出画面」占 {t_page - first:.2f}s")
    else:
        print("未观察到新进程（可能复用已有 WebView2 进程）")

    try:
        port = json.loads(RUNNING.read_text(encoding="utf-8"))["port"]
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open(f"http://127.0.0.1:{port}/api/window/close", data=b"", timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
