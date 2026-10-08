# -*- coding: utf-8 -*-
"""对照实验：本地 file:// 页面 vs 本地 http:// 页面，首帧差多少。

2026-10-03 已坐实：瓶颈在 WebView2 进程起来之后（1.21s→5.10s，3.9s 都在等画面）。
本脚本把「页面来源」这一个变量单独拎出来对比：
  A) file:// 直接加载同一个 HTML 模板
  B) http://127.0.0.1:<端口>/ 走 Flask
如果 A 很快、B 很慢 → 问题在「HTTP/Flask/端口」这条链上；
如果 A 也一样慢 → 问题在 WebView2 渲染这个页面本身（例如字体、SVG、CSS 某处）。

用法：
    python docs\\probes\\verify_pagesource_ab.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from douyin_tool.config import get_data_dir  # noqa: E402

PY = str(pathlib.Path(sys.executable))
TEMPLATE = ROOT / "web" / "templates" / "index.html"
TMP = pathlib.Path.home() / "AppData/Local/Temp/dyd_v2"
TMP.mkdir(parents=True, exist_ok=True)

PROBE = r'''
import sys, time, json
sys.path.insert(0, r"{root}\\src")
from douyin_tool import config as cfg_mod
import webview
url = {url!r}
prof = cfg_mod.get_data_dir() / "webview_profile"
webview.settings["DRAG_REGION_SELECTOR"]=".pywebview-drag-region"
t0 = time.time()
result = {{"url": url}}
w = webview.create_window("AB", url, width=1000, height=700, hidden=True)

page = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#1F1E1B;color:#ddd;font-family:sans-serif}}
#m{{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:#1F1E1B}}
</style></head><body>
<div id="m"><span style="color:#D97757">loading…</span></div>
</body></html>"""

def probe():
    # 等页面真的能 evaluate
    for _ in range(80):
        try:
            v = w.evaluate_js("Math.round(performance.now())")
            if isinstance(v, (int, float)):
                result["dom_ms"] = int(v)
                result["wall_to_eval_s"] = round(time.time() - t0, 2)
                print("RESULT " + json.dumps(result))
                return
        except Exception:
            pass
        time.sleep(0.1)
    result["error"] = "eval timeout"
    print("RESULT " + json.dumps(result))

webview.start(probe, private_mode=False, storage_path=str(prof))
'''


def run_case(name: str, url: str) -> None:
    print(f"\n--- {name} ---")
    if url.startswith("file:"):
        p = ROOT / "web" / "templates" / "index.html"
        target = p.resolve().as_uri()
    else:
        target = url
    code = PROBE.format(root=str(ROOT), url=target)
    t0 = time.time()
    try:
        r = subprocess.run([PY, "-c", code], capture_output=True, timeout=70)
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        for line in out.splitlines():
            if line.startswith("RESULT "):
                print("   ", line[7:])
        if "RESULT " not in out:
            print("    无结果；stderr 尾部：")
            for line in err.splitlines()[-6:]:
                print("     ", line)
    except subprocess.TimeoutExpired:
        print("    超时（70s）")
    print(f"    （外部实测总耗时 {time.time() - t0:.1f}s）")


def main() -> int:
    # A: file:// 直接开模板（不需要 Flask）
    run_case("A: file:// 直接加载 index.html", "file://x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
