# -*- coding: utf-8 -*-
"""无头 Chrome 验 ≤640px 断点。

为什么不用真窗口：`src/douyin_tool/win_window.py:87` 的 `MIN_W, MIN_H = 900, 600` 经 `:538`
的 `min_size=` 生效，窗口物理上窄不到 900px，所以 640px 以下的规则在桌面窗口里
根本触发不到（它只服务于「退回系统浏览器」那条回退路径）。这里用无头 Chrome
直接给视口，把几何结果写进 document.title，再用 --dump-dom 读回来。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SRC = ROOT / "web" / "templates" / "index.html"
# 探针页写在 probes/ 自己旁边，不写 build_tmp/ —— 后者是 PyInstaller 的工作目录，
# 往那儿丢文件会让「构建产物目录」里混进 HTML，且每次打包都被 --clean 清掉。
COPY = HERE / "probe_narrow.html"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

PROBE = r"""
<script>
window.addEventListener('load', function () {
  var cs = function (el) { return getComputedStyle(el); };
  var R  = function (el) { return el.getBoundingClientRect(); };
  var L = document.querySelector('.card--login');
  var D = document.querySelector('.card--dl');
  var T = document.querySelector('.card--tasks');
  if (!L || !D || !T) { document.title = 'PROBE' + JSON.stringify({fatal: 'card missing'}); return; }
  var lr = R(L), dr = R(D), tr = R(T);
  var out = {
    innerW: window.innerWidth,
    rootFont: +parseFloat(cs(document.documentElement).fontSize).toFixed(4),
    cols: cs(document.querySelector('.layout')).gridTemplateColumns.split(' ').length,
    loginPadTop: cs(L).paddingTop,
    dlPadTop: cs(D).paddingTop,
    tasksPadTop: cs(T).paddingTop,
    padAllEqual: cs(L).paddingTop === cs(D).paddingTop
                  && cs(D).paddingTop === cs(T).paddingTop,
    tops: [Math.round(lr.top), Math.round(dr.top), Math.round(tr.top)],
    orderOK: lr.top < dr.top && dr.top < tr.top,
    lefts: [Math.round(lr.left), Math.round(dr.left), Math.round(tr.left)],
    tipsCardGone: document.querySelector('.card--tips') === null,
    tipsUlGone: document.querySelector('.tips') === null,
    detMarginTop: cs(L.querySelector('details')).marginTop,
    hOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    docScrollW: document.documentElement.scrollWidth
  };
  document.title = 'PROBE' + JSON.stringify(out);
});
</script>
"""


def build_copy() -> None:
    html = SRC.read_text(encoding="utf-8")
    if "</body>" not in html:
        raise SystemExit("模板里找不到 </body>")
    html = html.replace("</body>", PROBE + "</body>", 1)
    COPY.write_text(html, encoding="utf-8")


def run(width: int, height: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="dyd_probe_") as udd:
        cmd = [
            str(CHROME), "--headless=new", f"--window-size={width},{height}",
            f"--user-data-dir={udd}", "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--virtual-time-budget=4000",
            "--dump-dom", COPY.as_uri(),
        ]
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    dom = p.stdout.decode("utf-8", "replace")
    m = re.search(r"<title>PROBE(.*?)</title>", dom, re.S)
    if not m:
        # 有些版本会把标题放在别处或转义，退一步找任意 PROBE{...}
        m2 = re.search(r"PROBE(\{.*?\})", dom, re.S)
        if not m2:
            raise SystemExit(f"{width}x{height} 读不到探针结果；DOM 前 400 字：\n{dom[:400]}")
        return json.loads(m2.group(1).replace("&quot;", '"'))
    return json.loads(m.group(1).replace("&quot;", '"'))


def check(w: int, g: dict) -> list[str]:
    bad = []
    if not g["tipsCardGone"]:
        bad.append("小贴士卡片仍在 DOM")
    if not g["tipsUlGone"]:
        bad.append(".tips 列表仍在 DOM")
    if g["hOverflow"]:
        bad.append(f"横向溢出 scrollW={g['docScrollW']} > innerW={g['innerW']}")
    if not g["orderOK"]:
        bad.append(f"单列顺序乱 tops={g['tops']}")
    if len(set(g["lefts"])) != 1:
        bad.append(f"三卡左边界不一致 {g['lefts']}")
    if g["cols"] != 1:
        bad.append(f"列数 {g['cols']} != 1")
    if g["detMarginTop"] in ("0px", "auto"):
        bad.append(f"details 上边距被压掉 marginTop={g['detMarginTop']}")
    if w <= 640:
        if g["padAllEqual"] is False:
            bad.append(f"≤640 三卡内边距不一致 "
                       f"{g['loginPadTop']}/{g['dlPadTop']}/{g['tasksPadTop']}")
        want = round(1.2857 * g["rootFont"], 1)
        got = float(g["loginPadTop"].rstrip("px"))
        if abs(got - want) > 1:
            bad.append(f"≤640 内边距 {got}px != 1.2857rem={want}px")
    else:
        want = round(1.7143 * g["rootFont"], 1)
        got = float(g["loginPadTop"].rstrip("px"))
        if abs(got - want) > 1:
            bad.append(f">640 内边距 {got}px != 1.7143rem={want}px")
    return bad


def main() -> int:
    build_copy()
    print(f"副本：{COPY}")
    failures = []
    for w, h in [(640, 900), (600, 900), (700, 900), (800, 900)]:
        try:
            g = run(w, h)
        except SystemExit as e:
            print(f"FAIL {w}x{h}: {e}")
            failures.append(f"{w}x{h}: {e}")
            continue
        bad = check(w, g)
        print(f"GEO {w}x{h} " + json.dumps(g, ensure_ascii=False))
        if bad:
            print(f"FAIL {w}x{h}: " + " | ".join(bad))
            failures.extend(f"[{w}x{h}] {b}" for b in bad)
        else:
            print(f"PASS {w}x{h}")
    print("SUMMARY " + json.dumps({"failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
