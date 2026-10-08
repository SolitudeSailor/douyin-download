# -*- coding: utf-8 -*-
"""一次性施加「删小贴士 / 登录与下载左右对调 / 两卡平齐」的 7 处改动。

设计要点：每处改动都断言「在全文里唯一命中」，任何一处不匹配就整体不写盘，
避免出现「改了 6 处、第 7 处静默没改」的半成品状态。
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TARGET = ROOT / "web" / "templates" / "index.html"

A_OLD = """  /* ============================================================
     小贴士
     ============================================================ */
  .tips{list-style:none;}
  .tips li{position:relative;padding-left:1.2143rem;font-size:0.8929rem;
    line-height:1.7;color:var(--text-2);margin-bottom:0.7857rem;}
  .tips li:last-child{margin-bottom:0;}
  .tips li::before{content:"";position:absolute;left:0.1429rem;top:0.5714rem;
    width:0.3571rem;height:0.3571rem;border-radius:50%;background:var(--accent);
    opacity:.55;}
  .tips strong{color:var(--text);font-weight:600;display:block;
    font-size:0.8929rem;margin-bottom:0.0714rem;}

"""

B_OLD = """  @media (min-width:1000px){
    .layout{
      grid-template-columns:minmax(0,1fr) var(--aside);
      grid-template-areas:
        "dl    login"
        "tasks tips";
    }
    .card--dl   {grid-area:dl;}
    .card--login{grid-area:login;}
    .card--tasks{grid-area:tasks;}
    .card--tips {grid-area:tips;}
  }
"""

B_NEW = """  @media (min-width:1000px){
    .layout{
      grid-template-columns:var(--aside) minmax(0,1fr);
      grid-template-areas:
        "login dl"
        "tasks tasks";
      align-items:stretch;
    }
    .card--dl   {grid-area:dl;}
    .card--login{grid-area:login;
      display:flex;flex-direction:column;}
    .card--tasks{grid-area:tasks;}
    /* 登录卡被拉高后，把富余空白吃到 details 之上，让它底边与右侧
       下载卡的收尾元素同线。必须 scoped 在 media 内：全局化会在窄屏
       把 details 的 margin-top:var(--sp-4) 压成 0，破坏窄屏间距。 */
    .card--login details{margin-top:auto;}
  }
"""

E_OLD = """    <!-- ============ 使用小贴士（右栏第二行） ============ -->
    <section class="card card--tips">
      <div class="card-head">
        <h2>
          <span class="ico" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M12 3.5a5.5 5.5 0 0 0-3.3 9.9c.5.4.8 1 .8 1.6v.5h5v-.5c0-.6.3-1.2.8-1.6A5.5 5.5 0 0 0 12 3.5Z"/><path d="M10 18.5h4"/></svg>
          </span>
          使用小贴士
        </h2>
      </div>
      <ul class="tips">
        <li><strong>增量下载</strong>下过的作品自动跳过；文件被删掉时会自动补下。</li>
        <li><strong>批量模式</strong>粘贴用户主页链接，按条数和日期区间筛选。</li>
        <li><strong>文件命名</strong>日期_作者_文案，并按作者分目录存放。</li>
        <li><strong>数据本地</strong>登录态与视频都只留在你自己电脑上。</li>
      </ul>
    </section>

"""

EDITS: list[tuple[str, str, str]] = [
    ("A 删 .tips 样式块", A_OLD, ""),
    ("B 宽屏 grid：对调 + tasks 跨行 + 等高", B_OLD, B_NEW),
    ("C 删 :214 登录卡专属内边距", "  .card--login,.card--tips{padding:1.4286rem;}\n", ""),
    ("D 删 :408 窄屏登录卡专属内边距", "    .card--login,.card--tips{padding:1.1429rem;}\n", ""),
    ("E 删「使用小贴士」整卡 HTML", E_OLD, ""),
    (
        "F1 修正登录卡位置注释",
        "<!-- ============ 登录（宽屏落右栏，窄屏排最前） ============ -->",
        "<!-- ============ 登录（宽屏落左栏，窄屏排最前） ============ -->",
    ),
    (
        "F2 修正任务卡位置注释",
        "<!-- ============ 任务进度（左栏第二行） ============ -->",
        "<!-- ============ 任务进度（宽屏跨整行） ============ -->",
    ),
    (
        "G 清掉唯一的行内 px 外边距",
        '<p class="hint" style="margin-bottom:14px">',
        '<p class="hint" style="margin-bottom:var(--sp-4)">',
    ),
]


def main() -> int:
    raw = TARGET.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    src = raw.decode("utf-8")
    print(f"目标：{TARGET}")
    print(f"换行：{'CRLF' if nl == chr(13) + chr(10) else 'LF'}    原始 {len(raw)} 字节 / {src.count(nl)} 行")
    print("-" * 72)

    text = src
    for name, old, new in EDITS:
        o = old.replace("\n", nl)
        n = new.replace("\n", nl)
        hits = text.count(o)
        if hits != 1:
            print(f"✗ {name}：命中 {hits} 次（期望恰好 1 次），已中止，未写盘。")
            return 2
        text = text.replace(o, n, 1)
        print(f"✓ {name}：命中 1 次，已替换")

    if text == src:
        print("✗ 内容没有变化，已中止。")
        return 3

    out = text.encode("utf-8")
    TARGET.write_bytes(out)
    print("-" * 72)
    print(f"已写盘：{len(raw)} → {len(out)} 字节（{len(out) - len(raw):+d}），"
          f"{src.count(nl)} → {text.count(nl)} 行")

    diff = difflib.unified_diff(
        src.splitlines(), text.splitlines(),
        fromfile="index.html (before)", tofile="index.html (after)", lineterm="", n=2,
    )
    print("\n".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
