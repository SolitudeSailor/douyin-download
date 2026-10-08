# -*- coding: utf-8 -*-
"""连续开关 exe 并用 CDP 逐轮体检 —— 逼出「打开后窗口一片空白」。

判定不靠屏幕抓图（会被别的窗口遮挡误导），而是问 WebView2 自己：
    readyState / .card 数量 / body 高度 / 截图颜色分布

用法：
    python docs\\probes\\repro_cdp.py [轮数]     # 默认 4 轮
输出：每轮一行结论 + 异常轮的截图（%TEMP%\\dyd_v2\\repro_cdp_N.png）
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
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from douyin_tool.config import get_data_dir  # noqa: E402

RUNNING = get_data_dir() / "running.json"
EXE = ROOT / "dist" / "douyin-tool.exe"
OUTDIR = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2"

CHECK_JS = r"""
(() => {
  const cs = getComputedStyle(document.body);
  const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect();
    return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
  return JSON.stringify({
    href: location.href,
    readyState: document.readyState,
    title: document.title,
    cards: document.querySelectorAll('.card').length,
    bodyTextLen: (document.body.innerText || '').length,
    bodyRect: r(document.body),
    bodyBg: cs.backgroundColor,
    innerW: window.innerWidth, innerH: window.innerHeight,
    styleSheets: document.styleSheets.length,
    errs: window.__dydErr || null
  });
})()
"""


def jget(path: str, port: int, timeout: float = 3):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(url: str, timeout: float = 4):
    req = urllib.request.Request(url, method="POST")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check(port: int, shot: pathlib.Path | None = None) -> dict:
    """连上 CDP 体检一次。"""
    from websockets.sync.client import connect
    targets = [t for t in jget("/json/list", port) if t.get("type") == "page"]
    if not targets:
        return {"ok": False, "why": "WebView2 里没有 page target"}
    t = targets[0]
    _id = [0]
    with connect(t["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024,
                 open_timeout=8) as ws:
        def cmd(method, **params):
            _id[0] += 1
            mid = _id[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == mid:
                    if "error" in m:
                        raise RuntimeError(m["error"])
                    return m.get("result", {})

        cmd("Runtime.enable")
        cmd("Runtime.evaluate", expression=(
            "window.__dydErr=[];"
            "window.addEventListener('error',e=>window.__dydErr.push(''+e.message));"
            "window.addEventListener('unhandledrejection',"
            "e=>window.__dydErr.push('rej:'+(e.reason&&e.reason.message||e.reason)));"
            "'ok'"))
        res = cmd("Runtime.evaluate", expression=CHECK_JS, returnByValue=True)
        d = json.loads(res["result"]["value"])
        d["target"] = t["url"]
        if shot is not None:
            s = cmd("Page.captureScreenshot", format="png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            shot.write_bytes(base64.b64decode(s["data"]))
            try:
                from PIL import Image
                im = Image.open(shot).convert("RGB")
                cnt = Counter(im.getdata())
                d["colors"] = len(cnt)
                d["top"] = [f"#{c[0]:02X}{c[1]:02X}{c[2]:02X}:{n*100.0/(im.size[0]*im.size[1]):.0f}%"
                            for c, n in cnt.most_common(3)]
                d["shot"] = str(shot)
            except Exception as e:  # noqa: BLE001
                d["colors"] = f"(Pillow 失败 {e})"
        d["ok"] = (d.get("readyState") == "complete" and d.get("cards", 0) > 0
                   and (d.get("bodyRect") or [0, 0, 0, 0])[3] > 100)
        return d


def wait_running(timeout: float = 40):
    t = time.time()
    while time.time() - t < timeout:
        try:
            return int(json.loads(RUNNING.read_text(encoding="utf-8"))["port"])
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    return None


def wait_gone(timeout: float = 40):
    t = time.time()
    while time.time() - t < timeout:
        if not RUNNING.exists():
            return round(time.time() - t, 2)
        time.sleep(0.2)
    return None


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    print(f"=== CDP 逐轮体检：{rounds} 轮 ===")
    print(f"exe {time.strftime('%H:%M:%S', time.localtime(EXE.stat().st_mtime))}"
          f"  {EXE.stat().st_size} 字节\n")

    # 清场
    try:
        info = json.loads(RUNNING.read_text(encoding="utf-8"))
        post(f"http://127.0.0.1:{info['port']}/api/window/close")
    except Exception:  # noqa: BLE001
        pass
    if RUNNING.exists():
        print(f"等旧实例退出… {wait_gone()}s\n")

    bad = 0
    for i in range(1, rounds + 1):
        cdp = 19230 + i
        t0 = time.time()
        env = dict(os.environ)
        env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={cdp}"
        p = subprocess.Popen([str(EXE)], env=env)
        app_port = wait_running()
        # 等 CDP 端口活过来（WebView2 初始化完）
        cdp_ok = False
        while time.time() - t0 < 40:
            try:
                jget("/json/version", cdp, timeout=1)
                cdp_ok = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        t_cdp = round(time.time() - t0, 2)
        if not cdp_ok:
            print(f"第 {i} 轮  × CDP 端口没起来（app_port={app_port}）")
            bad += 1
            continue

        # 等页面真正画完（最长 20s），期间每 0.5s 体检一次
        last = None
        t_ready = None
        while time.time() - t0 < 40:
            try:
                d = check(cdp)
                last = d
                if d["ok"]:
                    t_ready = round(time.time() - t0, 2)
                    break
            except Exception as e:  # noqa: BLE001
                last = {"ok": False, "why": f"CDP 体检异常：{type(e).__name__}: {e}"}
            time.sleep(0.5)

        tag = "正常" if (last or {}).get("ok") else "★异常"
        print(f"第 {i} 轮  [{t_cdp}s CDP 就绪] [{t_ready}s 页面就绪] {tag}")
        if last:
            print(f"        readyState={last.get('readyState')} cards={last.get('cards')} "
                  f"bodyH={(last.get('bodyRect') or [0,0,0,0])[3]} "
                  f"bodyBg={last.get('bodyBg')} textLen={last.get('bodyTextLen')} "
                  f"css={last.get('styleSheets')} inner={last.get('innerW')}x{last.get('innerH')}")
            if last.get("errs"):
                print(f"        JS 报错：{last['errs']}")
            if last.get("why"):
                print(f"        原因：{last['why']}")
        if not (last or {}).get("ok"):
            bad += 1
            try:
                d2 = check(cdp, OUTDIR / f"repro_cdp_{i}.png")
                print(f"        截图 → {d2.get('shot')} 颜色数={d2.get('colors')} "
                      f"主色={d2.get('top')}")
            except Exception as e:  # noqa: BLE001
                print(f"        截图失败：{e}")

        # 收尾：关掉，等它退干净
        try:
            post(f"http://127.0.0.1:{app_port}/api/window/close")
        except Exception as e:  # noqa: BLE001
            print(f"        关闭请求失败：{e}")
        gone = wait_gone()
        print(f"        退出耗时 {gone}s  pid={p.pid}")
        if gone is None:                       # 没退干净，强制收，免得影响下一轮
            subprocess.run(["taskkill", "/F", "/PID", str(p.pid)],
                           capture_output=True, check=False)
            time.sleep(1.0)

    print(f"\n=== {rounds} 轮里 {bad} 轮异常 ===")
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
