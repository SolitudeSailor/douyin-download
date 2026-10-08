# -*- coding: utf-8 -*-
"""验证「文案只取前 6 个字」以及由此得到的文件路径长度可控。

背景：f2 的 split_filename() 按加权长度判定超限、却按纯字符数截前 N 个，
      导致长文案作品落盘出一个截断且**没有扩展名**的残废文件。
对策：下载前把 data["desc"] 削成前 6 个字（core._short_desc）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from douyin_tool import core  # noqa: E402
from support import prepare_offline_f2

prepare_offline_f2()

# 本文件是「自带断言的独立脚本」，正确跑法是 `python tests/test_desc_truncate.py`。
# 声明 __test__=False 是为了不让 pytest 误收集 —— 函数名恰好是 test_*，
# 但它们直接接收零参、且用 print + assert 打结果，不是 pytest 用例。
__test__ = False

# f2 一被 import 就会往「当前工作目录」写 logs/，而本文件的用例里会 import 它。
# 先把工作目录切到数据目录，避免测试跑完在项目根凭空冒出一个 logs/。
core._prepare_f2_env()

# 复现用户截图里那条作品的真实文案（喵喵折 · 7613599571695963444）
LONG_DESC = (
    "2026全网最细心的笔记本清灰教程，只需要20块钱，自己动手给电脑清灰换硅脂，"
    "电脑性能提升30%，游戏帧数暴涨大几十帧，温度还从90多度降到了五十多度，"
    "那么这期视频，咱们要做"
)
LONG_NICKNAME = "喵喵折"


def test_short_desc_truncates_to_6():
    data = {"desc": LONG_DESC, "nickname": LONG_NICKNAME}
    core._short_desc(data)
    # "2026全网最细..." → "2026" 占 4 个字符，再加「全网」= 前 6 字
    assert data["desc"] == "2026全网", data["desc"]
    assert len(data["desc"]) == 6
    print(f"  [ok] desc 截断为 {data['desc']!r}（{len(data['desc'])} 字）")


def test_short_desc_keeps_original():
    data = {"desc": LONG_DESC}
    core._short_desc(data)
    assert data["desc_full"] == LONG_DESC, "原文案应保留在 desc_full"
    print("  [ok] 原文案保留在 desc_full")


def test_short_desc_short_text_untouched():
    data = {"desc": "短文案"}
    core._short_desc(data)
    assert data["desc"] == "短文案"
    print("  [ok] 短文案不被改动")


def test_short_desc_empty_safe():
    assert core._short_desc({"desc": ""})["desc"] == ""
    assert core._short_desc({"desc": None})["desc"] is None
    assert core._short_desc({}) == {}
    print("  [ok] 空值/缺字段不炸")


def test_short_desc_strips_hashtags():
    """话题符号先被剔除，再数满 6 个正文字（不是拿 # 占位）。"""
    data = {"desc": "#笔记本电脑 #笔记本清灰 #换硅脂 教程"}
    core._short_desc(data)
    assert "#" not in data["desc"], data["desc"]
    # "#笔记本电脑 #笔记本清灰 #换硅脂 教程" 去符号去空格 →
    # "笔记本电脑笔记本清灰换硅脂教程" 前 6 字 = "笔记本电脑笔"
    assert data["desc"] == "笔记本电脑笔", repr(data["desc"])
    print(f"  [ok] 话题符剔除后取 6 字 → {data['desc']!r}")


def test_short_desc_only_symbols_falls_back():
    """整条都是符号时不该产出空名字。"""
    data = {"desc": "###"}
    core._short_desc(data)
    assert data["desc"], "不应产出空文件名"
    print(f"  [ok] 全符号文案有兜底 → {data['desc']!r}")


def test_final_path_well_under_windows_limit():
    """关键验收：拼出 f2 真实文件名，整段路径必须远低于 260。"""
    from f2.apps.douyin.utils import format_file_name

    data = {
        "create_time": "2026-03-05 10-55-03",
        "nickname": LONG_NICKNAME,
        "aweme_id": "7613599571695963444",
        "desc": LONG_DESC,
    }
    core._short_desc(data)

    naming = "{create}_{nickname}_{desc}"
    base = format_file_name(naming, data)
    filename = f"{base}_video.mp4"
    # 模拟用户实际目录 —— 用真实数据目录解析，别硬编码盘符（换机器/换布局会失真）
    from douyin_tool import config as cfg_mod
    full = cfg_mod.get_data_dir() / "Download" / LONG_NICKNAME / filename

    print(f"  [info] 文件名 = {filename}")
    print(f"  [info] 文件名长度 = {len(filename)}")
    print(f"  [info] 完整路径长度 = {len(str(full))}")

    assert len(filename) < 200, f"文件名过长：{len(filename)}"
    assert len(str(full)) < 260, f"路径超过 Windows 上限：{len(str(full))}"
    assert filename.endswith(".mp4"), "后缀必须保留"
    print("  [ok] 文件名与完整路径均远低于 Windows 260 上限，后缀完好")


def test_f2_native_truncation_would_have_broken_it():
    """反证：不截断 desc 时，f2 自己产出的名字会逼近上限（这是事故根因）。"""
    from f2.apps.douyin.utils import format_file_name

    data = {
        "create_time": "2026-03-05 10-55-03",
        "nickname": LONG_NICKNAME,
        "aweme_id": "7613599571695963444",
        "desc": LONG_DESC,          # ← 故意不截断
    }
    base = format_file_name("{create}_{nickname}_{desc}", data)
    print(f"  [info] f2 原生截断后 base 长度 = {len(base)}")
    print(f"  [info] 原生 base 尾部 = ...{base[-20:]!r}")
    assert len(base) > 100, "应能复现 f2 会产出超长的名字"
    print("  [ok] 复现了 f2 原生截断会产出 100+ 字符的名字（事故根因）")


if __name__ == "__main__":
    tests = [
        test_short_desc_truncates_to_6,
        test_short_desc_keeps_original,
        test_short_desc_short_text_untouched,
        test_short_desc_empty_safe,
        test_short_desc_strips_hashtags,
        test_short_desc_only_symbols_falls_back,
        test_final_path_well_under_windows_limit,
        test_f2_native_truncation_would_have_broken_it,
    ]
    passed = 0
    for t in tests:
        print(f"\n▶ {t.__name__}")
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] {type(e).__name__}: {e}")
    print(f"\n{'=' * 50}\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
