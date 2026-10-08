# -*- coding: utf-8 -*-
"""下载历史 / 增量校验的离线测试 —— 不联网、不下载、不碰真实数据。

用法：
    python tests/test_history.py

背景（这是一次真实故障的回归测试）：
    用户下载完视频后手动删掉了 mp4，再下载时程序却提示「已存在，跳过」。
    根因是旧的 is_downloaded() 只查 download_history.json 里的 ID 列表，
    **从不检查文件是否真的还在磁盘上**。

    修复后：「已下载」的判定标准是 **记录里的文件此刻真的存在**。
    文件被删 → 判定为 stale → 重新下载。
    旧格式（只存 ID、没有路径）无法校验 → 整段丢弃，下一轮重新校验一遍自愈。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from douyin_tool import config as cfg_mod  # noqa: E402

# 本文件是「自带断言的独立脚本」，正确跑法是 `python tests/test_history.py`。
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
        print(f"  [FAIL] {name}  ->  got={got!r}  want={want!r}")


def _write_raw(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_raw(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. v1 → v2 迁移：只存 ID 的旧记录无法校验，必须丢弃
# ---------------------------------------------------------------------------
def test_v1_migration(root: Path, hist: Path):
    _write_raw(hist, {"one": ["111", "222"]})

    check("v1 旧记录读入后被丢弃", cfg_mod.load_history(hist), {})
    check("v1 条目判定为未下载（unrecorded）",
          cfg_mod.history_status(root, "one", "111", hist), "unrecorded")
    check("v1 条目 is_downloaded 为 False",
          cfg_mod.is_downloaded(root, "one", "111", hist), False)


# ---------------------------------------------------------------------------
# 2+3. 核心用例：文件在 → ok；文件被删 → stale
# ---------------------------------------------------------------------------
def test_file_exists_then_deleted(root: Path, hist: Path):
    cfg_mod.reset_history(path=hist)

    media = root / "喵喵折" / "2026-03-05 10-55-03_喵喵折_电脑清灰_video.mp4"
    cfg_mod.mark_downloaded(root, "one", {"7613599571695963444": str(media)}, hist)

    # 记录必须落成相对路径
    raw = _read_raw(hist)
    check("历史文件带 version=2", raw.get("version"), 2)
    stored = raw.get("one", {}).get("7613599571695963444")
    check("落盘的是相对路径", stored, "喵喵折/2026-03-05 10-55-03_喵喵折_电脑清灰_video.mp4")

    # 文件还没建出来 → 仍然算 stale
    check("记录在但文件不存在 → stale",
          cfg_mod.history_status(root, "one", "7613599571695963444", hist), "stale")

    # 真的把文件建出来 → 变成 ok
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")
    check("文件存在 → ok",
          cfg_mod.history_status(root, "one", "7613599571695963444", hist), "ok")
    check("文件存在 → is_downloaded 为 True",
          cfg_mod.is_downloaded(root, "one", "7613599571695963444", hist), True)
    check("recorded_path 返回绝对路径",
          cfg_mod.recorded_path(root, "one", "7613599571695963444", hist), str(media))

    # ★ 用户删除 mp4 —— 这正是用户报的场景
    media.unlink()
    check("★ 用户删掉文件后 → stale",
          cfg_mod.history_status(root, "one", "7613599571695963444", hist), "stale")
    check("★ 用户删掉文件后 → is_downloaded 为 False（会重新下载）",
          cfg_mod.is_downloaded(root, "one", "7613599571695963444", hist), False)


# ---------------------------------------------------------------------------
# 4. 多作用域互不干扰
# ---------------------------------------------------------------------------
def test_scopes_are_isolated(root: Path, hist: Path):
    cfg_mod.reset_history(path=hist)
    one_file = root / "A" / "a_video.mp4"
    post_file = root / "B" / "b_video.mp4"
    for p in (one_file, post_file):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    cfg_mod.mark_downloaded(root, "one", {"999": str(one_file)}, hist)
    cfg_mod.mark_downloaded(root, "post:MS4wLjABX", {"999": str(post_file)}, hist)

    raw = _read_raw(hist)
    check("两个 scope 都在", sorted(k for k in raw if k != "version"),
          ["one", "post:MS4wLjABX"])
    check("one 域命中", cfg_mod.is_downloaded(root, "one", "999", hist), True)
    check("post 域命中", cfg_mod.is_downloaded(root, "post:MS4wLjABX", "999", hist), True)
    check("未记录的作品为 False", cfg_mod.is_downloaded(root, "one", "888", hist), False)


# ---------------------------------------------------------------------------
# 5. 没有文件路径的记账请求必须被忽略（否则重现老 bug）
# ---------------------------------------------------------------------------
def test_mark_without_path_is_ignored(root: Path, hist: Path):
    cfg_mod.reset_history(path=hist)
    cfg_mod.mark_downloaded(root, "one", ["12345"], hist)
    check("只给 ID、不给路径 → 不记账",
          cfg_mod.history_status(root, "one", "12345", hist), "unrecorded")


# ---------------------------------------------------------------------------
# 6. 清空历史
# ---------------------------------------------------------------------------
def test_reset(root: Path, hist: Path):
    media = root / "C" / "c_video.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x")
    cfg_mod.mark_downloaded(root, "one", {"777": str(media)}, hist)
    check("清空前有记录", cfg_mod.is_downloaded(root, "one", "777", hist), True)

    cfg_mod.reset_history(path=hist)
    check("清空后无记录", cfg_mod.is_downloaded(root, "one", "777", hist), False)
    check("清空后文件仍写 version=2", _read_raw(hist).get("version"), 2)


# ---------------------------------------------------------------------------
# 7. 损坏 / 异常输入不炸
# ---------------------------------------------------------------------------
def test_robustness(root: Path, hist: Path):
    hist.write_text("{ 这不是合法 json", encoding="utf-8")
    check("损坏的历史文件 → 空历史", cfg_mod.load_history(hist), {})
    check("损坏后查询不抛异常",
          cfg_mod.history_status(root, "one", "1", hist), "unrecorded")

    _write_raw(hist, {"version": 2, "one": {"1": ""}, "bad": "字符串不是 dict"})
    check("空路径条目被过滤", cfg_mod.load_history(hist), {})


def main():
    print("=" * 66)
    print("下载历史 / 增量校验测试（离线）")
    print("=" * 66)

    global _FAIL
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # 每个用例用独立的根目录与历史文件，互不污染
        for name, fn in [
            ("[1] v1 → v2 迁移", test_v1_migration),
            ("[2] 文件存在 / 被删（核心）", test_file_exists_then_deleted),
            ("[3] 多作用域隔离", test_scopes_are_isolated),
            ("[4] 无路径记账被忽略", test_mark_without_path_is_ignored),
            ("[5] 清空历史", test_reset),
            ("[6] 健壮性", test_robustness),
        ]:
            case_root = base / name.split("]")[0].strip("[]") / "Download"
            case_root.mkdir(parents=True, exist_ok=True)
            case_hist = case_root.parent / "download_history.json"
            print(f"\n{name}")
            fn(case_root, case_hist)

    print("\n" + "=" * 66)
    total = _PASS + _FAIL
    print(f"结果：{_PASS}/{total} 通过" + (f"，{_FAIL} 失败" if _FAIL else "，全绿 ✅"))
    print("=" * 66)
    if _FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
