# -*- coding: utf-8 -*-
"""V-2：量「窗口按钮 / 图标」在三档视口下的渲染几何（改造前 vs 改造后）。

做法：对两份页面各注入同一段探针，放进临时目录（**不动交付物**），
用系统 Chrome 无头模式 --dump-dom 把测量结果从 <title> 里读回来。

  current = src/douyin_tool/web/templates/index.html（改造后）
  before  = docs/probes/index.html.before-winbtn  （改造前，对照组）

对照组必须放在 probes/ 里，不能放应用模板目录 —— 该目录会被
spec 的资源清单整个打进 exe，
放在里面等于把 43 KB 的历史快照塞进交付物。

运行：
    C:\\Users\\...\\envs\\default\\Scripts\\python.exe docs\\probes\\verify_buttons.py
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
CHROME = pathlib.Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

SOURCES = {
    "current": ROOT / "src" / "douyin_tool" / "web" / "templates" / "index.html",
    "before": ROOT / "docs" / "probes" / "index.html.before-winbtn",
}
VIEWPORTS = [1164, 1440, 1920]
THEME = "dark"

PROBE = r"""
<script>
(function(){
  function q(n){var m=new RegExp('[?&]'+n+'=([^&]*)').exec(location.search);return m?decodeURIComponent(m[1]):null;}
  function box(el){var r=el.getBoundingClientRect();return (Math.round(r.width*100)/100)+'x'+(Math.round(r.height*100)/100);}
  function probe(){
    var th=q('theme'); if(th){document.documentElement.setAttribute('data-theme',th);}
    document.body.classList.add('in-app-window');
    document.body.classList.add('win-maximized');
    void document.body.offsetHeight;
    var ctl=document.querySelector('.win-controls');
    var btns=[].slice.call(document.querySelectorAll('.win-controls button'));
    var svgs=[].slice.call(document.querySelectorAll('.win-controls button svg'));
    var res={
      vf:q('vf'),
      theme:document.documentElement.getAttribute('data-theme'),
      rootFont:getComputedStyle(document.documentElement).fontSize,
      ctlDisplay:ctl?getComputedStyle(ctl).display:'NA',
      ctlBorderLeft:ctl?getComputedStyle(ctl).borderLeftWidth:'NA',
      ctlGap:ctl?getComputedStyle(ctl).gap:'NA',
      btnCount:btns.length,
      btns:btns.map(function(b){var s=getComputedStyle(b);
        return 'box='+box(b)+'|pad='+s.padding+'|bw='+s.borderTopWidth+'|bg='+s.backgroundColor+'|r='+s.borderRadius+'|flex='+s.flex;}),
      svgCount:svgs.length,
      svgs:svgs.map(function(v){var s=getComputedStyle(v);
        return 'cls='+(v.getAttribute('class')||'noClass')+'|box='+box(v)+'|disp='+s.display+'|basis='+s.flexBasis+'|shrink='+s.flexShrink;}),
      svgVisible:svgs.filter(function(v){return getComputedStyle(v).display!=='none';}).map(function(v){
        return 'cls='+(v.getAttribute('class')||'noClass')+'|box='+box(v);})
    };
    document.title='PROBE='+JSON.stringify(res);
  }
  if(document.readyState==='complete'){probe();}
  else{window.addEventListener('load',function(){setTimeout(probe,150);});}
})();
</script>
</body>"""


def kv(s: str) -> dict:
    parts = s.split("|")
    d = {"box": parts[0].replace("box=", "")}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        d[k] = v
    return d


def build_copy(src: pathlib.Path, dst: pathlib.Path) -> None:
    html = src.read_text(encoding="utf-8")
    if "</body>" not in html:
        raise SystemExit(f"[错误] {src} 里找不到 </body>")
    dst.write_text(html.replace("</body>", PROBE, 1), encoding="utf-8")


def dump(path: pathlib.Path, vf: int, theme: str, tag: str) -> dict | None:
    work = pathlib.Path(tempfile.gettempdir()) / "dyd_v2"
    udd = work / f"udd_{tag}_{vf}"
    url = "file:///" + path.as_posix() + f"?vf={vf}&theme={theme}"
    cmd = [
        str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
        "--disable-extensions", "--hide-scrollbars",
        f"--user-data-dir={udd}",
        f"--window-size={vf},1000",
        "--virtual-time-budget=4000",
        "--dump-dom", url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        print(f"   x {tag}@{vf} Chrome 超时")
        return None
    out = r.stdout.decode("utf-8", "replace")
    m = re.search(r"<title>(.*?)</title>", out, re.S)
    if not m:
        print(f"   x {tag}@{vf} 没读到 title（dump 长度 {len(out)}）")
        return None
    raw = m.group(1)
    if not raw.startswith("PROBE="):
        print(f"   x {tag}@{vf} title 不是探针结果：{raw[:140]}")
        return None
    return json.loads(raw[len("PROBE="):])


def main() -> int:
    if not CHROME.is_file():
        print(f"[错误] 找不到 Chrome：{CHROME}")
        return 2
    work = pathlib.Path(tempfile.gettempdir()) / "dyd_v2"
    work.mkdir(parents=True, exist_ok=True)

    copies = {}
    for tag, src in SOURCES.items():
        if not src.is_file():
            print(f"[错误] 源文件不存在：{src}")
            return 2
        dst = work / f"{tag}.html"
        build_copy(src, dst)
        copies[tag] = dst
        print(f"[准备] {tag:8s} <- {src.name}  ({src.stat().st_size} 字节)")

    results: dict = {}
    for tag, dst in copies.items():
        for vf in VIEWPORTS:
            res = dump(dst, vf, THEME, tag)
            results[f"{tag}@{vf}"] = res
            if not res:
                print(f"[测量] {tag:8s} @{vf:<5d} 失败")
                continue
            print(f"[测量] {tag:8s} @{vf:<5d} rootFont={res['rootFont']:<10s} "
                  f"ctl(disp={res['ctlDisplay']}, borderLeft={res['ctlBorderLeft']}, gap={res['ctlGap']})")
            for i, b in enumerate(res["btns"]):
                print(f"         按钮{i}: {b}")
            for i, s in enumerate(res["svgs"]):
                print(f"         svg{i}: {s}")
            print(f"         可见图标: {res['svgVisible']}")

    (work / "result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 判定 ----------------
    print("\n" + "=" * 74)
    print("  判定")
    print("=" * 74)
    ok = True

    for vf in VIEWPORTS:
        r_ = results.get(f"current@{vf}")
        if not r_:
            ok = False
            continue
        if r_["ctlDisplay"] != "flex":
            ok = False
            print(f"   x current@{vf}: .win-controls 没显示（disp={r_['ctlDisplay']}）")
        btns = [kv(b) for b in r_["btns"]]
        good = (len(btns) == 3 and all(
            b["box"] == "30x30" and b["pad"] == "0px" and b["bw"] == "0px"
            and b["bg"] == "rgba(0, 0, 0, 0)" for b in btns))
        if good:
            print(f"   v current@{vf}: 3 颗按钮均 30x30 / pad=0px / bw=0px / 透明底")
        else:
            ok = False
            print(f"   x current@{vf}: 按钮盒/内边距/边框/底色不符 -> {r_['btns']}")
        if r_["ctlBorderLeft"] != "0px":
            ok = False
            print(f"   x current@{vf}: 左侧竖线还在 border-left={r_['ctlBorderLeft']}")
        else:
            print(f"   v current@{vf}: 无左侧竖线（border-left=0px）")
        vis = [kv(v) for v in r_["svgVisible"]]
        if len(vis) == 3 and all(v["box"] == "15x15" for v in vis):
            print(f"   v current@{vf}: 3 个可见图标均 15x15")
        else:
            ok = False
            print(f"   x current@{vf}: 可见图标不是 3 个 15x15 -> {r_['svgVisible']}")

    print("\n   ---- 对照组（改造前）----")
    for vf in VIEWPORTS:
        r_ = results.get(f"before@{vf}")
        if not r_:
            ok = False
            continue
        b0 = r_["btns"][0] if r_["btns"] else "NA"
        v0 = r_["svgs"][0] if r_["svgs"] else "NA"
        print(f"   before@{vf}: 按钮 {b0}")
        print(f"                首个 svg {v0}   | borderLeft={r_['ctlBorderLeft']}")

    print("\n" + "-" * 74)
    print("  图标渲染尺寸对照")
    print("-" * 74)
    print(f"   {'视口':<8}{'改造前 首个svg':<26}{'改造后 可见svg'}")
    for vf in VIEWPORTS:
        a = results.get(f"before@{vf}")
        b = results.get(f"current@{vf}")
        a_s = kv(a["svgs"][0])["box"] if a and a["svgs"] else "NA"
        b_s = ", ".join(kv(v)["box"] for v in b["svgVisible"]) if b else "NA"
        print(f"   {vf:<8}{a_s:<26}{b_s}")

    print("\n   结论：" + ("v 全部通过" if ok else "x 有项目未通过"))
    print(f"   原始数据：{work / 'result.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
