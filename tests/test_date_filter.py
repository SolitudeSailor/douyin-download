# -*- coding: utf-8 -*-
"""日期筛选回归测试 —— 纯离线，不联网、不下载。

用法：
    python tests/test_date_filter.py

背景（这是一次真实故障的回归测试，2026-09-29）：
    用户在 Web 界面选了「起始日期 2026/09/28、结束日期 2026/09/29」，
    结果 20 条全给下了，从 2026-08-15 一直到 2026-09-29，日期筛选形同虚设。

    根因是**对 create_time 类型判断错误**：
      f2 的 `UserPostFilter.create_time`（f2/apps/douyin/filter.py:160-165）
      返回的不是 unix 时间戳，而是
        timestamp_2_str(str(ct))  →  "%Y-%m-%d %H-%M-%S"
      格式化后的**字符串**，例如 "2026-08-15 11-53-15"
      （这也正是下载文件名里那串时间）。

      旧实现：
        ts = int(create_time)                      # ← 对字符串抛 ValueError
        except (TypeError, ValueError): return True # ← 吞掉异常，判定"在区间内"
      于是每一条都被放行 —— 过滤器 100% 失效。

    修复：
      1. 新增 `_parse_create_date()`，同时认「数字时间戳（秒/毫秒/数字串）」与
         「日期字符串（f2 默认格式 + ISO 等变体）」两类形态；
      2. `download_user_posts()` 里再用 f2 的 cursor（interval → min/max_cursor）
         在服务端层面先把请求范围收窄；
      3. 列表按时间倒序，遇到早于起始日期的作品就在页末收手，少翻无用页。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from douyin_tool import core  # noqa: E402

# 本文件是「自带断言的独立脚本」，正确跑法是 `python tests/test_date_filter.py`。
# __test__=False 阻止 pytest 误收集（函数名恰好是 test_*，但签名不是 pytest 风格）。
__test__ = False

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  [ok]   {name}  ->  {got}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  ->  期望 {want}，实际 {got}")


def test_in_date_range():
    """核心：区间判断必须能消化 f2 的字符串形态。"""
    print("日期区间判断（create_time 是 f2 的格式化字符串）")

    # ★ 故障现场复现：这条曾经被判为"在区间内"
    check("08-15 不在 09-28~09-29",
          core._in_date_range("2026-08-15 11-53-15", "2026-09-28", "2026-09-29"),
          False)
    check("09-29 在 09-28~09-29",
          core._in_date_range("2026-09-29 13-05-07", "2026-09-28", "2026-09-29"),
          True)
    # 边界：含首尾当天
    check("起始当天 00-00-00 算在内",
          core._in_date_range("2026-09-28 00-00-00", "2026-09-28", "2026-09-29"),
          True)
    check("结束当天 23-59-59 算在内",
          core._in_date_range("2026-09-29 23-59-59", "2026-09-28", "2026-09-29"),
          True)
    check("结束次日 00-00-00 排除",
          core._in_date_range("2026-09-30 00-00-00", "2026-09-28", "2026-09-29"),
          False)
    check("起始前一日 23-59-59 排除",
          core._in_date_range("2026-09-27 23-59-59", "2026-09-28", "2026-09-29"),
          False)

    # 单边区间
    check("只给起始日期：早于它排除",
          core._in_date_range("2026-09-18 19-29-06", "2026-09-28", None), False)
    check("只给起始日期：晚于它保留",
          core._in_date_range("2026-09-29 13-05-07", "2026-09-28", None), True)
    check("只给结束日期：早于它保留",
          core._in_date_range("2026-09-18 19-29-06", None, "2026-09-29"), True)
    check("只给结束日期：晚于它排除",
          core._in_date_range("2026-09-30 10-00-00", None, "2026-09-29"), False)
    check("两端都不给：全部保留",
          core._in_date_range("2026-01-01 00-00-00", None, None), True)


def test_other_shapes():
    """兼容其它形态：数字时间戳、ISO 字符串。"""
    print()
    print("兼容其它 create_time 形态")

    # 1755841200 = 2025-08-22 12:20:00 (UTC+8)，属于更早的月份
    check("int 秒级时间戳",
          core._in_date_range(1755841200, "2026-09-28", "2026-09-29"), False)
    check("str 秒级时间戳",
          core._in_date_range("1755841200", "2026-09-28", "2026-09-29"), False)
    check("ISO 日期串",
          core._in_date_range("2026-09-28", "2026-09-28", "2026-09-29"), True)
    check("ISO 日期时间串",
          core._in_date_range("2026-09-28T10:00:00", "2026-09-28", "2026-09-29"),
          True)
    # 解析不出来时放行（宁可多留，不可误杀），属于异常路径
    check("无法解析则放行",
          core._in_date_range("不是时间", "2026-09-28", "2026-09-29"), True)
    check("None 则放行",
          core._in_date_range(None, "2026-09-28", "2026-09-29"), True)


def test_before_range():
    """倒序列表的提前收手判断。"""
    print()
    print("越过起始日期的收手判断")

    check("08-15 早于 09-28 → 该收手",
          core._before_range("2026-08-15 11-53-15", "2026-09-28"), True)
    check("09-29 不早于 09-28 → 继续",
          core._before_range("2026-09-29 13-05-07", "2026-09-28"), False)
    check("起始当天不算越界",
          core._before_range("2026-09-28 00-00-00", "2026-09-28"), False)
    check("没给起始日期就永不收手",
          core._before_range("2026-09-28 00-00-00", None), False)
    check("解析不了不收手",
          core._before_range("不是时间", "2026-09-28"), False)


def test_page_filter():
    """模拟真实一页 20 条（取自用户故障截图里的文件名时间），
    验证按 09-28~09-29 过滤后只剩区间内的作品。"""
    print()
    print("整页过滤（模拟用户那一页 20 条）")

    page = [
        ("2026-08-15 11-53-15", False),
        ("2026-08-17 11-17-52", False),
        ("2026-08-27 12-21-29", False),
        ("2026-08-31 23-09-28", False),
        ("2026-09-05 12-25-42", False),
        ("2026-09-07 14-03-35", False),
        ("2026-09-08 11-44-33", False),
        ("2026-09-10 18-05-01", False),
        ("2026-09-11 12-04-54", False),
        ("2026-09-16 18-36-03", False),
        ("2026-09-18 10-40-38", False),
        ("2026-09-18 19-29-06", False),
        ("2026-09-20 12-12-20", False),
        ("2026-09-20 17-40-37", False),
        ("2026-09-21 12-46-25", False),
        ("2026-09-21 17-41-29", False),
        ("2026-09-22 13-01-16", False),
        ("2026-09-26 10-33-31", False),
        ("2026-09-27 19-10-55", False),
        ("2026-09-29 13-05-07", True),
    ]

    kept = [ct for ct, _ in page
            if core._in_date_range(ct, "2026-09-28", "2026-09-29")]
    want = [ct for ct, ok in page if ok]

    check("区间内条数", len(kept), len(want))
    check("区间内作品", kept, want)
    check("修复前会下满 20 条", len(kept) != len(page), True)


def main():
    test_in_date_range()
    test_other_shapes()
    test_before_range()
    test_page_filter()

    print()
    print(f"  通过 {_PASS} / 共 {_PASS + _FAIL}")
    if _FAIL:
        print(f"  {_FAIL} 项失败 ❌")
        return 1
    print("  全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
