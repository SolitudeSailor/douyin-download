# -*- coding: utf-8 -*-
"""用 CDP 直连 WebView2，检查「窗口里到底画了什么」—— 白屏排查的可靠取证手段。

为什么不用屏幕抓图：
    BitBlt 抓屏幕要求目标窗口真在前台，自动化环境里 SetForegroundWindow 常被
    前台锁定静默拒绝，结果抓到的是**遮挡它的别的窗口**（实测抓到过编辑器界面），
    结论全是错的。CDP 的 Page.captureScreenshot 是 WebView2 自己渲染出来的位图，
    完全不受遮挡、前台、DPI 影响。

依赖 exe 带调试端口启动：
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=19222 ./dist/douyin-tool.exe

用法：
    python docs\\probes\\cdp_probe.py [端口]        # 默认 19222
输出：控制台报告 + %TEMP%\\dyd_v2\\cdp_shot.png
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19222
OUT = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2" / "cdp_shot.png"

CHECK_JS = r"""
(() => {
  const q = (s) => !!document.querySelector(s);
  const cards = document.querySelectorAll('.card').length;
  const header = document.querySelector('header');
  const cs = getComputedStyle(document.body);
  const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect();
    return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
  return JSON.stringify({
    href: location.href,
    readyState: document.readyState,
    title: document.title,
    theme: document.documentElement.getAttribute('data-theme'),
    bodyClass: document.body.className,
    bodyTextLen: (document.body.innerText || '').length,
    htmlLen: document.documentElement.outerHTML.length,
    cards: cards,
    hasLoginBtn: q('#loginBtn'),
    headerRect: r(header),
    loginCardRect: r(document.querySelector('.card')),
    bodyRect: r(document.body),
    htmlRect: r(document.documentElement),
    bodyBg: cs.backgroundColor,
    bodyOpacity: cs.opacity,
    bodyDisplay: cs.display,
    bodyVisibility: cs.visibility,
    innerW: window.innerWidth, innerH: window.innerHeight,
    dpr: window.devicePixelRatio,
    visState: document.visibilityState,
    scrollH: document.documentElement.scrollHeight,
    styleSheets: document.styleSheets.length,
    scripts: document.scripts.length,
    jsErrors: window.__dydErr || null
  });
})()
"""


def http_json(path: str):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    try:
        ver = http_json("/json/version")
        print(f"CDP 在线：{ver['Browser']}")
    except Exception as e:  # noqa: BLE001
        print(f"× 连不上调试端口 {PORT}：{e}")
        print("  启动时要带 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=…")
        return 1

    targets = [t for t in http_json("/json/list") if t.get("type") == "page"]
    if not targets:
        print("× 没有 page 类型的 target（WebView2 里连页面都没有）")
        return 1
    t = targets[0]
    print(f"页面 target：{t['title']}  {t['url']}")

    from websockets.sync.client import connect  # noqa: E402

    _id = [0]

    with connect(t["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        def cmd(method: str, **params):
            _id[0] += 1
            mid = _id[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method} 失败: {msg['error']}")
                    return msg.get("result", {})

        cmd("Runtime.enable")

        # 先装一个全局错误钩子，再让它跑一会儿 —— 能抓到「渲染中途报错」
        cmd("Runtime.evaluate", expression=(
            "window.__dydErr=[];"
            "window.addEventListener('error',e=>window.__dydErr.push('error:'+e.message));"
            "window.addEventListener('unhandledrejection',"
            "e=>window.__dydErr.push('reject:'+(e.reason&&e.reason.message||e.reason)));"
            "'hooked'"))

        res = cmd("Runtime.evaluate", expression=CHECK_JS, returnByValue=True)
        data = json.loads(res["result"]["value"])
        print("\n=== 页面状态 ===")
        for k, v in data.items():
            print(f"  {k:16} = {v}")

        # 单位：把「有没有内容」变成可比较的几何量
        cards, body_rect = data.get("cards"), data.get("bodyRect")
        print("\n=== 判定 ===")
        problems = []
        if data["readyState"] != "complete":
            problems.append(f"页面没加载完（{data['readyState']}）")
        if not cards:
            problems.append("一个 .card 都没有")
        if not data["hasLoginBtn"]:
            problems.append("登录按钮不存在")
        if body_rect and body_rect[3] < 100:
            problems.append(f"body 高度只有 {body_rect[3]}px")
        if data.get("jsErrors"):
            problems.append(f"JS 报错：{data['jsErrors']}")
        print("  " + ("；".join(problems) if problems else "页面结构与尺寸都正常"))

        shot = cmd("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_bytes(base64.b64decode(shot["data"]))
        print(f"\nWebView2 自绘截图 → {OUT}  ({OUT.stat().st_size} 字节)")

        # 顺带量一下这张图的内容丰富度，和屏幕抓图对照
        try:
            from collections import Counter
            from PIL import Image
            im = Image.open(OUT).convert("RGB")
            cnt = Counter(im.getdata())
            print(f"截图尺寸 {im.size}，不同颜色数 {len(cnt)}")
            for c, n in cnt.most_common(4):
                print(f"   #{c[0]:02X}{c[1]:02X}{c[2]:02X}  "
                      f"{n * 100.0 / (im.size[0] * im.size[1]):5.1f}%")
        except Exception as e:  # noqa: BLE001
            print(f"（Pillow 统计跳过：{e}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
