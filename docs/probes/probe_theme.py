# -*- coding: utf-8 -*-
"""探针：量化「明暗切换延迟」与「最大化时文本是否扩张」。

走真实页面 + 真实无边框窗口，不碰生产代码。
产出 JSON：窗口两种尺寸下的布局快照 + 12 次主题切换的耗时/颜色同步性。
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


SNAP_JS = r"""
JSON.stringify((() => {
  const wrap  = document.querySelector('.wrap');
  const h1    = document.querySelector('h1');
  const cards = document.querySelectorAll('.card, section');
  const cs    = el => el ? getComputedStyle(el) : null;
  return {
    innerW:  window.innerWidth,
    innerH:  window.innerHeight,
    wrapW:   wrap ? Math.round(wrap.getBoundingClientRect().width) : null,
    wrapMax: cs(wrap) ? cs(wrap).maxWidth : null,
    bodyFont: getComputedStyle(document.body).fontSize,
    h1Font:   h1 ? cs(h1).fontSize : null,
    firstParaFont: cs(document.querySelector('p, .hint, label'))
                     ? cs(document.querySelector('p, .hint, label')).fontSize : null,
    cardCount: cards.length,
    cardW: cards[0] ? Math.round(cards[0].getBoundingClientRect().width) : null,
    nodes: document.querySelectorAll('*').length,
    // 水平溢出检测：放大后有没有元素被挤出窗口
    hOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    docScrollW: document.documentElement.scrollWidth,
    overflowEls: (() => {
      const bad = [];
      document.querySelectorAll('body *').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.right > window.innerWidth + 1) {
          bad.push((el.tagName + '.' + (el.className || '')).slice(0, 34));
        }
      });
      return bad.slice(0, 6);
    })(),
  };
})())
"""

BENCH_JS = r"""
window.__bench = null;
(async () => {
  const raf   = () => new Promise(r => requestAnimationFrame(r));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const bg    = el => el ? getComputedStyle(el).backgroundColor : null;
  const body  = document.body;
  const card  = document.querySelector('.card') || document.querySelector('section')
                || document.querySelector('.wrap > *');

  const out = {nodes: document.querySelectorAll('*').length, samples: []};

  for (let i = 0; i < 12; i++) {
    const wasDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const t0 = performance.now();
    toggleTheme();                      // 真实入口：设属性 + 存 localStorage + 换图标
    const tCall = performance.now();
    void body.offsetHeight;             // 强制样式重算 + 布局
    const tRecalc = performance.now();

    await raf();                        // 第一帧：绘制完成
    const tF1 = performance.now();
    const bodyF1 = bg(body), cardF1 = bg(card);

    await raf();                        // 第二帧
    const tF2 = performance.now();

    out.samples.push({
      i,
      call:   +(tCall  - t0).toFixed(2),   // 光是 JS 调用（setAttribute 立刻返回？）
      recalc: +(tRecalc- t0).toFixed(2),   // 含同步样式重算 + 布局
      frame1: +(tF1    - t0).toFixed(2),   // 含首帧绘制
      frame2: +(tF2    - t0).toFixed(2),
      // 第一帧时两者的颜色：不同 => 有元素在渐变、有元素已跳变（视觉分层）
      bodyMid: bodyF1, cardMid: cardF1,
      bodyFinal: bg(body), cardFinal: bg(card),
      target: wasDark ? 'light' : 'dark',
    });
    await sleep(400);                   // 让 0.2s 过渡彻底跑完，样本互不干扰
  }
  window.__bench = JSON.stringify(out);
})();
"""


def js_json(win, js: str, timeout: float = 20.0, interval: float = 0.12):
    """执行 JS 并解析它返回的 JSON 字符串。"""
    for _ in range(int(timeout / interval)):
        raw = win.evaluate_js(js)
        if raw:
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return raw
        time.sleep(interval)
    return None


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = make_server("127.0.0.1", port, web_app.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"URL {url}", flush=True)

    def on_ready(win) -> None:                          # noqa: ANN001
        def work() -> None:
            try:
                # 等页面真正加载出 DOM
                for _ in range(80):
                    if win.evaluate_js("!!document.querySelector('.wrap')"):
                        break
                    time.sleep(0.25)
                time.sleep(1.5)

                win_window.focus_window()               # rAF 在后台窗口会被节流
                try:
                    win.resize(1164, 821)
                except Exception:                       # noqa: BLE001
                    pass
                time.sleep(1.5)
                snap_normal = js_json(win, SNAP_JS)

                try:
                    win.resize(1920, 1032)
                except Exception:                       # noqa: BLE001
                    pass
                time.sleep(1.5)
                snap_max = js_json(win, SNAP_JS)

                # 再缩回去，验证是双向跟随而不是只放大不缩回
                try:
                    win.resize(1164, 821)
                except Exception:                       # noqa: BLE001
                    pass
                time.sleep(1.5)
                snap_back = js_json(win, SNAP_JS)

                win.evaluate_js(BENCH_JS)
                bench = js_json(win, "window.__bench",
                                timeout=40.0, interval=0.25)

                print("SNAP_NORMAL " + json.dumps(snap_normal, ensure_ascii=False),
                      flush=True)
                print("SNAP_MAX " + json.dumps(snap_max, ensure_ascii=False),
                      flush=True)
                print("SNAP_BACK " + json.dumps(snap_back, ensure_ascii=False),
                      flush=True)
                print("BENCH " + json.dumps(bench, ensure_ascii=False), flush=True)
            except Exception as e:                      # noqa: BLE001
                print(f"PROBE_ERROR {type(e).__name__}: {e}", flush=True)
            finally:
                try:
                    win.destroy()
                except Exception:                       # noqa: BLE001
                    pass

        threading.Thread(target=work, daemon=True).start()

    win_window.run_window(url, "探针", fallback_log=False, on_ready=on_ready)
    srv.shutdown()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
