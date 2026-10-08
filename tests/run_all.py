# -*- coding: utf-8 -*-
"""一键跑完全部测试脚本，最后汇总结果。

用法：
    python tests/run_all.py

为什么不用 pytest：
    这些脚本是「自带断言的独立脚本」—— 函数名恰好像 pytest 用例（test_*），
    但签名是零参 + 自己 print 结果，pytest 收集后会报 fixture 缺失。
    每个文件里都写了 `__test__ = False` 明确禁止 pytest 收集。
"""

from __future__ import annotations

import subprocess
import sys
import os
import tempfile
from pathlib import Path

__test__ = False  # 本文件也不是 pytest 用例

HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parent / "src"

# 顺序有讲究：先跑离线、快的，把需要占端口起服务的放最后
SCRIPTS = [
    "test_agents_document",  # 协作规范
    "test_git_tracking",    # 凭据、运行数据和构建产物的忽略规则
    "test_github_release",  # GitHub 发布配置与可移植性
    "test_notes_demo",      # 视频笔记 Demo 信息架构与关键交互
    "test_link_parse",       # 链接解析（纯离线）
    "test_date_filter",      # 日期区间筛选（纯离线，f2 字符串形态回归）
    "test_post_date_flow",   # 主页批量+日期区间全流程（纯离线，假 f2 handler）
    "test_history",          # 增量历史 + 文件级校验（纯离线，临时目录）
    "test_system_integrity", # 原子持久化、并发、强制下载与取消
    "test_desc_truncate",    # 文案截断与文件名长度（会 import f2）
    "test_web_api",          # Web API（起真实 Flask 服务，127.0.0.1:8815）
    "test_lifecycle",        # 关页面即退出（起真实子进程，127.0.0.1:8821，约 25 秒）
]


def _run_suite() -> int:
    results = []

    for name in SCRIPTS:
        script = HERE / f"{name}.py"
        print()
        print("=" * 66)
        print(f"  {name}")
        print("=" * 66)
        try:
            code = subprocess.run([sys.executable, "-X", "utf8", str(script)],
                                  cwd=HERE.parent, timeout=150).returncode
        except subprocess.TimeoutExpired:
            print(f"  [错误] {name} 超过 150 秒，已停止。")
            code = 1
        except OSError as e:
            print(f"  [错误] 无法运行 {script}: {e}")
            code = 1
        results.append((name, code))

    print()
    print("=" * 66)
    print("  汇总")
    print("=" * 66)
    failed = 0
    for name, code in results:
        mark = "通过" if code == 0 else f"失败（退出码 {code}）"
        if code != 0:
            failed += 1
        print(f"  {'✔' if code == 0 else '✘'}  {name:<24} {mark}")

    print()
    if failed:
        print(f"  {failed}/{len(results)} 个脚本失败 ❌")
    else:
        print(f"  全部 {len(results)} 个脚本通过 ✅")
    return 1 if failed else 0


def main() -> int:
    # 每次运行使用全新的数据目录，子进程继承，绝不改真实 Cookie 或历史。
    previous = os.environ.get("DOUYIN_DATA_DIR")
    previous_pythonpath = os.environ.get("PYTHONPATH")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SOURCE_ROOT), previous_pythonpath) if part
    )
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="dyd_suite_") as temp:
            os.environ["DOUYIN_DATA_DIR"] = temp
            return _run_suite()
    finally:
        if previous is None:
            os.environ.pop("DOUYIN_DATA_DIR", None)
        else:
            os.environ["DOUYIN_DATA_DIR"] = previous
        if previous_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous_pythonpath


if __name__ == "__main__":
    sys.exit(main())
