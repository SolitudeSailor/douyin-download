# -*- coding: utf-8 -*-
"""验证「重复缓存副本不再堆积」—— no-store 头是否真的挡住了 WebView2 的磁盘缓存。

背景（2026-10-03）：
    data/webview_profile/EBWebView/Default/Cache/Cache_Data/ 里堆了 53 个 f_* 文件，
    其中 27 个大小完全相同（51568 字节），内容都是 src/douyin_tool/web/templates/index.html ——
    因为服务每次绑随机端口 = 每次新 origin，Flask 又不发 Cache-Control，
    WebView2 就把同一个页面按 origin 各存一份。

    旧 profile（堆积 50+ 份）→ WebView2 首帧 3306ms
    全新 profile            → WebView2 首帧  169ms

本脚本：
    1) 记下当前 Cache_Data 里的 index.html 副本数
    2) 启动一次应用（源码态）
    3) 关闭后再数一遍 —— 装了 no-store 之后不应该增加

用法：
    python docs\\probes\\verify_cache_nostore.py
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

CACHE = get_data_dir() / "webview_profile" / "EBWebView" / "Default" / "Cache" / "Cache_Data"
PAGE_SIZE = 51568          # 无 no-store 时 index.html 的缓存体大小（实测）
PY = str(pathlib.Path(sys.executable))


def count_index_copies() -> tuple[int, int]:
    """返回（f_* 文件数, 疑似 index.html 副本数）。"""
    if not CACHE.exists():
        return 0, 0
    fs = list(CACHE.glob("f_*"))
    same = [f for f in fs if f.stat().st_size == PAGE_SIZE]
    return len(fs), len(same)


def main() -> int:
    before = count_index_copies()
    print(f"启动前：f_* 文件 {before[0]} 个，其中 {before[1]} 个是 index.html 副本"
          f"（{PAGE_SIZE} 字节）")

    t0 = time.time()
    p = subprocess.Popen([PY, "-m", "douyin_tool"], cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等 HTTP 真正可用（running.json 出现 ≠ 服务已在监听）
    running = get_data_dir() / "running.json"
    port = None
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    cc = None
    while time.time() - t0 < 60:
        try:
            port = json.loads(running.read_text(encoding="utf-8"))["port"]
            with op.open(f"http://127.0.0.1:{port}/", timeout=3) as r:
                cc = r.headers.get("Cache-Control")
            break
        except Exception:                # noqa: BLE001
            time.sleep(0.3)
    print(f"[{time.time() - t0:5.2f}s] 服务就绪，端口 {port}")
    print(f"           响应头 Cache-Control: {cc}")

    time.sleep(3.0)                      # 让 WebView2 有机会去写缓存

    try:
        op.open(f"http://127.0.0.1:{port}/api/window/close", data=b"", timeout=5)
    except Exception:                    # noqa: BLE001
        pass
    try:
        p.wait(timeout=20)
    except Exception:                    # noqa: BLE001
        p.kill()
    time.sleep(1.5)

    after = count_index_copies()
    print(f"关闭后：f_* 文件 {after[0]} 个，其中 {after[1]} 个是 index.html 副本")

    delta = after[1] - before[1]
    print("\n★ 结论：")
    print(f"   index.html 缓存副本增加 {delta} 份")
    if "no-store" in (cc or "") and delta <= 0:
        print("   ✅ no-store 生效 —— 启动一次不再新增重复副本")
        return 0
    print("   ❌ 仍在新增 —— no-store 没挡住，需要继续排查")
    return 1


if __name__ == "__main__":
    sys.exit(main())
