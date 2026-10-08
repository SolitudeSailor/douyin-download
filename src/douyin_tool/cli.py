# -*- coding: utf-8 -*-
"""抖音视频下载器 —— 统一入口。

用法：
  python -m douyin_tool                 # 启动 Web 界面（默认）
  python -m douyin_tool --no-browser    # 启动但不自动开浏览器
  python -m douyin_tool -u "<链接>"      # 命令行直接下载单个作品
  python -m douyin_tool -u "<主页链接>" -M post   # 批量下载主页
  python -m douyin_tool -f urls.txt -M one -o out # 从清单文件批量下载

打包成 exe 后，双击即等同 `python -m douyin_tool`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许以 `python -m douyin_tool` 或冻结后直接运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from douyin_tool import config as cfg_mod
from douyin_tool import console
from douyin_tool import core
from douyin_tool import validation

# 尽早接管输出：无控制台（--noconsole 打包）时避免 print 崩溃
console.init_console()
console.install_excepthook()

print = console.safe_print  # noqa: A001  统一走安全输出


def main() -> int:
    parser = argparse.ArgumentParser(
        description="抖音视频下载器（Web 界面 + 命令行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--web", action="store_true", help="启动 Web 界面（默认行为）")
    parser.add_argument("--no-browser", action="store_true", help="启动 Web 但不自动打开浏览器")
    parser.add_argument("--port", type=int, default=0, help="Web 端口，0 表示自动选择")
    parser.add_argument("-u", "--url", help="抖音链接（作品或主页）")
    parser.add_argument("-f", "--file", help="链接清单文件（每行一条）")
    parser.add_argument("-M", "--mode", choices=["one", "post"], default="one",
                        help="下载模式：one=单个作品，post=用户主页")
    parser.add_argument("-o", "--out", help="下载目录")
    parser.add_argument("-k", "--cookie", help="抖音 Cookie")
    parser.add_argument("-P", "--proxy", help="代理，如 http://127.0.0.1:7890")
    parser.add_argument("-n", "--naming", help="文件名模板")
    parser.add_argument("--max-counts", type=int, default=None, help="最大下载数，0=不限；省略使用配置")
    parser.add_argument("--date-start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--date-end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--no-skip", action="store_true", help="不跳过已下载的作品")
    parser.add_argument("--login", action="store_true",
                        help="打开专属浏览器窗口登录抖音（自动保存 Cookie）")

    args = parser.parse_args()
    try:
        args.max_counts = validation.nonnegative_int(args.max_counts, default=None)
        args.date_start, args.date_end = validation.date_range(args.date_start, args.date_end)
        if args.naming:
            validation.naming_template(args.naming)
        if args.proxy:
            validation.proxy_url(args.proxy)
        if not 0 <= args.port <= 65535:
            raise ValueError("端口必须在 0 到 65535 之间。")
    except ValueError as exc:
        parser.error(str(exc))

    # ---- 登录：打开浏览器窗口，扫码后自动保存 Cookie ----
    if args.login:
        return _run_login()

    # ---- 更新配置 ----
    updates = {}
    if args.out:
        updates["download_dir"] = args.out
    if args.cookie:
        updates["cookie"] = args.cookie
    if args.proxy:
        updates["proxy"] = args.proxy
    if args.naming:
        updates["naming"] = args.naming
    if updates:
        cfg_mod.update_config(**updates)

    cfg = cfg_mod.load_config()

    # ---- 有链接走命令行下载 ----
    links = []
    if args.url:
        links.append(args.url)
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                links.extend(line.strip() for line in f if line.strip())
        except OSError as e:
            print(f"[错误] 读取清单文件失败：{e}", file=sys.stderr)
            return 1

    if links:
        return _run_cli(links, args, cfg)

    # ---- 否则启动 Web ----
    from douyin_tool.web_app import run_server
    run_server(port=args.port, open_browser=not args.no_browser)
    return 0


def _run_login() -> int:
    """命令行登录：打开专属浏览器窗口，等用户扫码，自动保存 Cookie。"""
    from douyin_tool import browser_login as bl

    print("正在启动专属浏览器窗口，请在窗口里登录抖音…")
    cookie, msg = bl.fetch_cookie(interactive=True)
    if cookie and bl.is_logged_in(cookie):
        cfg_mod.update_config(cookie=cookie, cookie_updated_at=int(__import__("time").time()))
        print(f"✓ {msg}")
        print(f"  登录态已保存：{cfg_mod.get_config_path()}")
        print("  之后直接粘贴网址即可下载，无需再登录。")
        return 0
    print(f"✗ {msg}", file=sys.stderr)
    return 1


def _run_cli(links, args, cfg) -> int:
    """命令行批量下载。"""
    if not cfg.get("cookie"):
        print("提示：尚未登录抖音，多数接口会被风控拒绝（403）。"
              "请先运行 `--login` 完成一次性登录。\n", file=sys.stderr)

    total_ok = total_fail = 0

    for idx, link in enumerate(links, 1):
        print(f"\n[{idx}/{len(links)}] {link}")

        def on_progress(cur, tot, msg):
            print(f"    {msg}")

        try:
            if args.mode == "post":
                result = core.run_async(core.download_user_posts(
                    link, cfg, on_progress=on_progress,
                    max_counts=args.max_counts,
                    date_start=args.date_start, date_end=args.date_end,
                    skip_downloaded=not args.no_skip,
                ))
                print(f"    ✓ 完成：下载 {result['download_count']} 条，"
                      f"跳过 {len(result['skipped'])} 条，"
                      f"失败 {len(result['failed'])} 条")
                if result["failed"]:
                    total_fail += 1
                    continue
            else:
                result = core.run_async(core.download_one(
                    link, cfg, on_progress=on_progress,
                    skip_downloaded=not args.no_skip,
                ))
                if result.get("skipped"):
                    print("    - 已下载过，跳过")
                else:
                    if not result.get("path"):
                        raise core.DouyinError("没有生成视频文件。")
                    print(f"    ✓ 完成：{result.get('path')}")
            total_ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"    ✗ 失败：{e}", file=sys.stderr)
            total_fail += 1

    print(f"\n汇总：成功 {total_ok}，失败 {total_fail}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
