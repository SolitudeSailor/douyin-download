# -*- coding: utf-8 -*-
"""验证启动遮罩：① 不依赖脚本也能显示 ② 主界面就绪后会被移除 ③ 视觉正确。

用无头 Chrome 的 CDP 连上去，分别在「禁用 JS」和「启用 JS」两种模式下截图，
一次把两个特性都测到。

用法：
    python docs\\probes\\verify_bootmask.py [url]     # 默认 http://127.0.0.1:8899/
输出：%TEMP%\\dyd_v2\\mask_nojs.png（遮罩态）/ mask_ready.png（就绪态）
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899/"
PORT = 19311
OUT = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2"
CHROME = pathlib.Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PROFILE = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_chrome_probe"


def jget(path: str):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if PROFILE.exists():
        import shutil
        shutil.rmtree(PROFILE, ignore_errors=True)

    ch = subprocess.Popen([
        str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
        "--window-size=1164,821", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        for _ in range(50):
            try:
                jget("/json/version")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            print("× 无头 Chrome 没起来")
            return 1

        from websockets.sync.client import connect

        ws_url = [t for t in jget("/json/list") if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        _id = [0]

        with connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            def cmd(method, **params):
                _id[0] += 1
                mid = _id[0]
                ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    m = json.loads(ws.recv())
                    if m.get("id") == mid:
                        if "error" in m:
                            raise RuntimeError(f"{method}: {m['error']}")
                        return m.get("result", {})

            cmd("Page.enable")
            cmd("Runtime.enable")

            def snapshot(name: str, js_disabled: bool) -> dict:
                cmd("Emulation.setScriptExecutionDisabled", value=js_disabled)
                cmd("Page.navigate", url=URL)
                time.sleep(2.5 if not js_disabled else 1.6)
                d: dict = {}
                try:
                    info = cmd("Runtime.evaluate", expression=(
                        "JSON.stringify({mask: !!document.getElementById('bootMask'),"
                        " cards: document.querySelectorAll('.card').length})"
                    ), returnByValue=True)
                    d = json.loads(info["result"]["value"])
                except Exception as e:  # noqa: BLE001
                    d = {"mask": f"(evaluate 不可用: {e})", "cards": None}
                s = cmd("Page.captureScreenshot", format="png")
                p = OUT / name
                p.write_bytes(base64.b64decode(s["data"]))
                d["shot"] = str(p)
                return d

            nojs = snapshot("mask_nojs.png", True)
            print(f"【禁用 JS】DOM 断言：{nojs}")

            ready = snapshot("mask_ready.png", False)
            print(f"【启用 JS】DOM 断言：{ready}")

            print("\n=== 判定 ===")
            ok = True
            if ready.get("mask"):
                print("  × JS 就绪后遮罩还在（应该被移除）")
                ok = False
            else:
                print("  ✓ JS 就绪后遮罩已移除")
            if ready.get("cards"):
                print(f"  ✓ 主界面卡片 {ready['cards']} 个")
            else:
                print(f"  × 没找到主界面卡片：{ready.get('cards')}")
                ok = False
            try:
                from collections import Counter
                from PIL import Image
                for tag, f in (("禁用JS", "mask_nojs.png"), ("就绪", "mask_ready.png")):
                    im = Image.open(OUT / f).convert("RGB")
                    cnt = Counter(im.getdata())
                    print(f"  {tag}截图 {im.size} 颜色数 {len(cnt)} "
                          f"主色 {['#%02X%02X%02X:%.0f%%' % (c[0],c[1],c[2],n*100.0/(im.size[0]*im.size[1])) for c,n in cnt.most_common(3)]}")
            except Exception as e:  # noqa: BLE001
                print(f"  （颜色统计跳过：{e}）")
            return 0 if ok else 2
    finally:
        ch.terminate()
        time.sleep(0.6)
        if ch.poll() is None:
            ch.kill()


if __name__ == "__main__":
    sys.exit(main())
