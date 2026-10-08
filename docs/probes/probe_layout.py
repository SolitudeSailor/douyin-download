# -*- coding: utf-8 -*-
"""探针：量化「删小贴士 / 登录与下载左右对调 / 两卡平齐」后的真实几何。

走真实页面 + 真实无边框窗口，不碰生产代码。
四个视口各量一次：宽屏 1164 / 1200（两栏），窄屏 900 / 600（单列）。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, str(pathlib.Path(_ROOT) / "src"))

from douyin_tool import core                                    # noqa: E402

core._prepare_f2_env()                                  # noqa: SLF001

from douyin_tool import web_app, win_window                     # noqa: E402
from werkzeug.serving import make_server                # noqa: E402

GEO_JS = r"""
JSON.stringify((() => {
  const cs = el => getComputedStyle(el);
  const R  = el => el.getBoundingClientRect();
  const L = document.querySelector('.card--login');
  const D = document.querySelector('.card--dl');
  const T = document.querySelector('.card--tasks');
  const lay = document.querySelector('.layout');
  if (!L || !D || !T || !lay) return JSON.stringify({fatal: 'card missing'});
  const lr = R(L), dr = R(D), tr = R(T);
  const lt = R(L.querySelector('.card-head h2'));
  const dt = R(D.querySelector('.card-head h2'));
  const det = L.querySelector('details');
  const fs = parseFloat(cs(document.documentElement).fontSize);
  const areas = cs(lay).gridTemplateAreas;
  const cols  = cs(lay).gridTemplateColumns.split(' ').map(s => parseFloat(s));
  return JSON.stringify({
    innerW: window.innerWidth, innerH: window.innerHeight,
    rootFont: +fs.toFixed(4),
    colCount: cols.length,
    col1: Math.round(cols[0]),
    col1Want: Math.round(24.2857 * fs),
    areas: areas,
    twoCol: areas.indexOf('login dl') >= 0,
    login: {left: Math.round(lr.left), top: Math.round(lr.top),
            bottom: Math.round(lr.bottom), w: Math.round(lr.width), h: Math.round(lr.height)},
    dl:    {left: Math.round(dr.left), top: Math.round(dr.top),
            bottom: Math.round(dr.bottom), w: Math.round(dr.width), h: Math.round(dr.height)},
    tasks: {left: Math.round(tr.left), top: Math.round(tr.top),
            bottom: Math.round(tr.bottom), w: Math.round(tr.width)},
    titleTopDiff:  Math.round(Math.abs(lt.top - dt.top)),
    bottomDiff:    Math.round(Math.abs(lr.bottom - dr.bottom)),
    titleLeftDelta: Math.round(lt.left - dt.left),
    detBottomGap: det ? Math.round((lr.bottom - parseFloat(cs(L).paddingBottom)) - R(det).bottom) : null,
    detMarginTop: det ? cs(det).marginTop : null,
    docScrollW: document.documentElement.scrollWidth,
    hOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    overflowEls: (() => {
      const bad = [];
      document.querySelectorAll('body *').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.right > window.innerWidth + 1) {
          bad.push((el.tagName + '.' + (el.className || '')).slice(0, 40));
        }
      });
      return bad.slice(0, 6);
    })(),
    tipsCardGone: document.querySelector('.card--tips') === null,
    tipsUlGone:   document.querySelector('.tips') === null,
  });
})())
"""


def js_json(win, js: str, timeout: float = 20.0, interval: float = 0.12):
    """执行 JS 并拿到 Python 对象。

    注意：pywebview 的 evaluate_js 对「返回 JSON 字符串」的表达式会做一次额外编码，
    直接 json.loads 一次只解开一层、拿到 str，所以要循环解到不再是 str 为止。
    """
    for _ in range(int(timeout / interval)):
        raw = win.evaluate_js(js)
        if raw:
            for _ in range(4):
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                        continue
                    except (TypeError, ValueError):
                        break
                break
            return raw
        time.sleep(interval)
    return None


# 注意：src/douyin_tool/win_window.py:87 `MIN_W, MIN_H = 900, 600` 经 :538 `min_size=` 生效，
# 窗口物理上窄不到 900px，所以更小的视口在这里量不到（≤640px 那段只能靠无头 Chrome 验）。
VIEWPORTS = [(1164, 900), (1200, 1000), (900, 700)]


def check(name: str, g: dict) -> list[str]:
    """返回失败项列表。"""
    bad: list[str] = []
    wide = g["innerW"] >= 1000

    def a(cond, msg):
        if not cond:
            bad.append(msg)

    a(g["tipsCardGone"], "小贴士卡片仍在 DOM")
    a(g["tipsUlGone"], ".tips 列表仍在 DOM")
    a(not g["hOverflow"], f"横向溢出 docScrollW={g['docScrollW']} > innerW={g['innerW']}")
    a(not g["overflowEls"], f"越界元素 {g['overflowEls']}")

    if wide:
        a(g["twoCol"], f"grid-template-areas 不含 'login dl'：{g['areas']}")
        a(g["areas"].count("tasks") >= 2, f"tasks 未跨整行：{g['areas']}")
        a(g["login"]["left"] < g["dl"]["left"],
          f"登录未在左：login.left={g['login']['left']} dl.left={g['dl']['left']}")
        a(g["bottomDiff"] <= 1, f"两卡底边不齐：差 {g['bottomDiff']}px")
        a(g["titleTopDiff"] <= 1, f"两卡标题不同线：差 {g['titleTopDiff']}px")
        a(abs(g["col1"] - g["col1Want"]) <= 1,
          f"窄列宽 {g['col1']}px != 24.2857×{g['rootFont']}={g['col1Want']}px")
        a(g["colCount"] == 2, f"宽屏列数 {g['colCount']} != 2")
        pair_bottom = max(g["login"]["bottom"], g["dl"]["bottom"])
        a(g["tasks"]["top"] >= pair_bottom - 1,
          f"任务卡未在两卡之下：tasks.top={g['tasks']['top']} < {pair_bottom}")
        span = g["login"]["w"] + g["dl"]["w"]
        a(g["tasks"]["w"] >= span - 2, f"任务卡未跨整行：w={g['tasks']['w']} < {span}")
        a(g["detBottomGap"] is not None and abs(g["detBottomGap"]) <= 2,
          f"details 未贴底：差 {g['detBottomGap']}px")
    else:
        a(not g["twoCol"], f"窄屏仍是两栏：{g['areas']}")
        a(g["login"]["top"] < g["dl"]["top"] < g["tasks"]["top"],
          f"单列顺序乱：top = {g['login']['top']} / {g['dl']['top']} / {g['tasks']['top']}")
        a(abs(g["login"]["left"] - g["dl"]["left"]) <= 1
          and abs(g["dl"]["left"] - g["tasks"]["left"]) <= 1,
          f"单列三卡左边界不一致：{g['login']['left']} / {g['dl']['left']} / {g['tasks']['left']}")
        mt = g["detMarginTop"]
        a(mt not in (None, "0px", "auto"),
          f"窄屏 details 上边距被压掉：marginTop={mt}")
        a(g["colCount"] == 1, f"窄屏列数 {g['colCount']} != 1")
    return bad


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = make_server("127.0.0.1", port, web_app.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"URL {url}", flush=True)

    results: dict[str, dict] = {}
    failures: list[str] = []

    def on_ready(win) -> None:                          # noqa: ANN001
        def work() -> None:
            try:
                for _ in range(80):
                    if win.evaluate_js("!!document.querySelector('.layout')"):
                        break
                    time.sleep(0.25)
                time.sleep(1.5)
                win_window.focus_window()               # rAF/布局在后台窗口会被节流

                for w, h in VIEWPORTS:
                    try:
                        win.resize(w, h)
                    except Exception:                   # noqa: BLE001
                        pass
                    time.sleep(1.2)
                    g = js_json(win, GEO_JS)
                    key = f"{w}x{h}"
                    results[key] = g
                    print(f"GEO {key} " + json.dumps(g, ensure_ascii=False), flush=True)
                    if not isinstance(g, dict):
                        failures.append(f"[{key}] 探针没返回字典：{type(g).__name__}")
                        print(f"FAIL {key}: 探针没返回字典", flush=True)
                        continue
                    if g.get("fatal"):
                        failures.append(f"[{key}] {g['fatal']}")
                        print(f"FAIL {key}: {g['fatal']}", flush=True)
                        continue
                    try:
                        bad = check(key, g)
                    except Exception as e:              # noqa: BLE001
                        bad = [f"断言函数自身出错 {type(e).__name__}: {e}"]
                    if bad:
                        for m in bad:
                            failures.append(f"[{key}] {m}")
                        print(f"FAIL {key}: " + " | ".join(bad), flush=True)
                    else:
                        print(f"PASS {key}", flush=True)
            except Exception as e:                      # noqa: BLE001
                print(f"PROBE_ERROR {type(e).__name__}: {e}", flush=True)
            finally:
                print("SUMMARY " + json.dumps(
                    {"viewports": list(results), "failures": failures},
                    ensure_ascii=False), flush=True)
                try:
                    win.destroy()
                except Exception:                       # noqa: BLE001
                    pass

        threading.Thread(target=work, daemon=True).start()

    win_window.run_window(url, "布局探针", fallback_log=False, on_ready=on_ready)
    srv.shutdown()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
