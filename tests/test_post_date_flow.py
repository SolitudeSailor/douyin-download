# -*- coding: utf-8 -*-
"""主页批量 + 日期区间：全流程集成测试（离线，不联网、不下载）。

用法：
    python tests/test_post_date_flow.py

为什么要单独做这一层：
    `test_date_filter.py` 只验了 `_in_date_range()` 这个小函数。
    而这次故障"看起来是过滤器坏了"，链路里其实还有三件事同等重要：

      ① f2 的 `create_time` 是**字符串** —— 由 fake 数据原样复现；
      ② `fetch_user_post_videos(min_cursor=...)` 会让 f2 崩在
         `nickname_raw` 上（见 core.py 的注释与 f2 handler.py:417/431/455）；
      ③ 有日期区间时 `max_counts` 必须交给本地判断，否则区间外的作品
         也会吃掉配额、区间内的反而下不全。

    这三条只有把整条链路真跑一遍才暴露得出来，
    所以这里用 fake 的 f2 handler 驱动 `core.download_user_posts()`。

⚠️ 假数据必须遵守**接口的真实形态**：作品列表是**按时间倒序**的
   （最新在前）。顺序搞错会让"越过起始日期就收手"的判断看起来是错的。
   下面 PAGE_DESC 的 20 条时间戳取自用户故障截图里的真实文件名，倒序排列。
"""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from douyin_tool import core, config as cfg_mod  # noqa: E402
from support import prepare_offline_f2

prepare_offline_f2()

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


# ---------------------------------------------------------------------------
# 假数据
# ---------------------------------------------------------------------------
# 用户那一页（20 条，倒序 = 最新在前，与接口一致）
PAGE_DESC = [
    ("7690100000000000020", "2026-09-29 13-05-07", "探索_喝水"),        # ★ 唯一在区间内
    ("7690100000000000019", "2026-09-27 19-10-55", "探索_象群喝水"),
    ("7690100000000000018", "2026-09-26 10-33-31", "探索_狮群"),
    ("7690100000000000017", "2026-09-22 13-01-16", "探索_草原泥"),
    ("7690100000000000016", "2026-09-21 17-41-29", "探索_即将消失的"),
    ("7690100000000000015", "2026-09-21 12-46-25", "探索_石头缝里"),
    ("7690100000000000014", "2026-09-20 17-40-37", "探索_仿真鲨鱼"),
    ("7690100000000000013", "2026-09-20 12-12-20", "探索_世界上最安"),
    ("7690100000000000012", "2026-09-18 19-29-06", "探索_螺旋桨上的"),
    ("7690100000000000011", "2026-09-18 10-40-38", "探索_这样的混凝"),
    ("7690100000000000010", "2026-09-16 18-36-03", "探索_老旧油画上"),
    ("7690100000000000009", "2026-09-11 12-04-54", "探索_小蜂窝"),
    ("7690100000000000008", "2026-09-10 18-05-01", "探索_猞猁浑身是刺"),
    ("7690100000000000007", "2026-09-08 11-44-33", "探索_非洲象喜欢"),
    ("7690100000000000006", "2026-09-07 14-03-35", "探索_藏獒是如何"),
    ("7690100000000000005", "2026-09-05 12-25-42", "探索_分享6种很有"),
    ("7690100000000000004", "2026-08-31 23-09-28", "探索_野猪跑到这里"),
    ("7690100000000000003", "2026-08-27 12-21-29", "探索_分享一些冷门"),
    ("7690100000000000002", "2026-08-17 11-17-52", "探索_大象洗澡"),
    ("7690100000000000001", "2026-08-15 11-53-15", "探索_牛犊子喝奶"),
]

IN_RANGE_ID = "7690100000000000020"


def _gen_page(prefix, n, day_from=29):
    """生成 n 条倒序数据，日期从 09-{day_from} 往前（每两条一天）。"""
    rows = []
    for i in range(n):
        day = day_from - i // 2
        rows.append((f"{prefix}{i:04d}", f"2026-09-{day:02d} 12-00-00", f"探索_{i}"))
    return rows


# 跨页用例：第一页 20 条全部落在 [09-20, 09-29]，第二页整体越界
PAGE_FULL_A = _gen_page("768800000000000%04d" % 0, 20, day_from=29)
PAGE_FULL_B = [
    ("768800000000000900", "2026-09-15 12-00-00", "探索_更早的A"),
    ("768800000000000901", "2026-09-10 12-00-00", "探索_更早的B"),
]


class _FakePage:
    """模拟 f2 的 UserPostFilter。

    ★ 关键：`_to_list()` 里 create_time 用**字符串**，
      与 f2 的 `timestamp_2_str()` 输出完全一致 —— 这正是故障根因所在。
      用时间戳做假数据会让这个测试失去意义。
    """

    def __init__(self, rows):
        self._rows = rows

    def _to_list(self):
        return [
            {
                "aweme_id": aid,
                "create_time": ct,          # ← 字符串，不是时间戳
                "desc": desc,
                "desc_full": desc,
                "nickname": "果子",
            }
            for aid, ct, desc in self._rows
        ]


class _FakeHandler:
    """记录 core 到底给 f2 传了什么参数，并回放假数据页。

    ⚠️ `fetch_user_post_videos` 是**一个 async generator 对应一次调用**，
       它内部自己循环 yield 多页（真实 f2 也是这个结构）。
       所以 `calls` 永远只有 1 条；「翻了几页」要看 `pages_yielded`。
       另外 async generator 的函数体**延迟到第一次 anext 才执行**，
       调用方法本身不会触发任何副作用。
    """

    pages = [PAGE_DESC]          # 测试里按需替换
    instances = []

    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.calls = []              # 这次「方法调用」收到的参数（恒 1 条）
        self.pages_yielded = 0       # ★ 实际翻了几页
        _FakeHandler.instances.append(self)

    async def fetch_user_post_videos(self, sec_user_id, min_cursor=0,
                                     max_cursor=0, page_counts=20,
                                     max_counts=None):
        self.calls.append({
            "min_cursor": min_cursor,
            "max_cursor": max_cursor,
            "page_counts": page_counts,
            "max_counts": max_counts,
        })
        for rows in type(self).pages:
            self.pages_yielded += 1
            yield _FakePage(rows)


async def _fake_save(kwargs, data, base_dir, nickname="", desc=""):
    """假装落盘成功，不碰网络。"""
    p = base_dir / f"{data['aweme_id']}_video.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")   # 写点内容，让增量校验认为文件在
    return str(p)


def _run(cfg, pages, **kw):
    """跑一次 download_user_posts，返回 (结果, handler 实例)。"""
    import f2.apps.douyin.handler as f2_handler

    _FakeHandler.pages = pages
    _FakeHandler.instances = []
    real_handler = f2_handler.DouyinHandler
    real_save = core._save_video
    f2_handler.DouyinHandler = _FakeHandler
    core._save_video = _fake_save
    try:
        res = asyncio.run(core.download_user_posts(
            "https://www.douyin.com/user/MS4wLjABAAAA_q89peXG7c-dLG9x9Q-gVUmyYlwp",
            cfg, **kw,
        ))
    finally:
        f2_handler.DouyinHandler = real_handler
        core._save_video = real_save
    return res, (_FakeHandler.instances or [None])[0]


def _make_cfg(tmpdir):
    cfg = dict(cfg_mod.DEFAULT_CONFIG)
    cfg["download_dir"] = tmpdir
    cfg["cookie"] = "test=1"
    return cfg


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_replay_user_failure():
    """★ 故障现场回放：区间 09-28~09-29，那一页只有 1 条落区间内。"""
    print("回放故障：区间 2026-09-28 ~ 2026-09-29（上限 20）")

    tmpdir = tempfile.mkdtemp(prefix="dyd_dateflow_")
    try:
        res, handler = _run(_make_cfg(tmpdir), [PAGE_DESC],
                            max_counts=20,
                            date_start="2026-09-28", date_end="2026-09-29",
                            skip_downloaded=False)

        check("下载条数（修复前是 20）", res["download_count"], 1)
        check("下载的正是区间内那一条",
              [d["aweme_id"] for d in res["downloaded"]], [IN_RANGE_ID])
        check("失败条数", len(res["failed"]), 0)

        # ★ 关键回归：min_cursor 必须恒为 0。
        #   传非 0 会让 f2 在 handler.py:417 第一轮就 break，
        #   进而在循环外读未赋值的 nickname_raw，整个任务直接崩。
        check("传给 f2 的 min_cursor 恒为 0", handler.calls[0]["min_cursor"], 0)
        # 有区间时不能把 max_counts 交给 f2（否则区间外作品也吃配额）
        check("有区间时不把 max_counts 交给 f2",
              handler.calls[0]["max_counts"], None)
        # 第 2 条（09-27）就已越界 → 本页跑完就收手，不再要下一页
        check("只拉了 1 页（越界后收手）", handler.pages_yielded, 1)

        files = [f for f in os.listdir(tmpdir) if f.endswith(".mp4")]
        check("磁盘上的视频文件数", len(files), 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_cross_page():
    """区间够宽时能跨页找：第 1 页全在区间内 → 继续第 2 页 → 越界收手。"""
    print()
    print("跨页查找：区间 2026-09-20 ~ 2026-09-29（上限 25）")

    tmpdir = tempfile.mkdtemp(prefix="dyd_dateflow_")
    try:
        res, handler = _run(_make_cfg(tmpdir), [PAGE_FULL_A, PAGE_FULL_B],
                            max_counts=25,
                            date_start="2026-09-20", date_end="2026-09-29",
                            skip_downloaded=False)

        check("下载条数（首页 20 条全在区间内）", res["download_count"], 20)
        check("翻了 2 页后被越界拦下", handler.pages_yielded, 2)
        check("min_cursor", handler.calls[0]["min_cursor"], 0)
        check("max_counts 仍未交给 f2", handler.calls[0]["max_counts"], None)
        check("第 2 页的 2 条越界作品未被下载",
              len(res["downloaded"]) + len(res["skipped"]), 20)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_no_date_range_keeps_old_behavior():
    """不传日期区间时，行为与修复前完全一致。"""
    print()
    print("不带日期区间：max_counts 原样交给 f2，不额外翻页")

    tmpdir = tempfile.mkdtemp(prefix="dyd_dateflow_")
    try:
        res, handler = _run(_make_cfg(tmpdir), [PAGE_DESC],
                            max_counts=3, skip_downloaded=False)

        check("下载条数（取前 3 条）", res["download_count"], 3)
        check("min_cursor", handler.calls[0]["min_cursor"], 0)
        check("max_counts 原样透传", handler.calls[0]["max_counts"], 3)
        check("只拉了 1 页", handler.pages_yielded, 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_single_side_range():
    """只给起始日期：由条数上限收手。"""
    print()
    print("只给起始日期 2026-01-01（上限 5）")

    tmpdir = tempfile.mkdtemp(prefix="dyd_dateflow_")
    try:
        res, handler = _run(_make_cfg(tmpdir), [PAGE_DESC],
                            max_counts=5,
                            date_start="2026-01-01", skip_downloaded=False)

        check("下载条数（被上限截断）", res["download_count"], 5)
        check("min_cursor", handler.calls[0]["min_cursor"], 0)
        check("max_counts 不交给 f2", handler.calls[0]["max_counts"], None)
        check("只拉了 1 页", handler.pages_yielded, 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    core._prepare_f2_env()      # 先切好目录，再 import f2 相关模块

    test_replay_user_failure()
    test_cross_page()
    test_no_date_range_keeps_old_behavior()
    test_single_side_range()

    print()
    print(f"  通过 {_PASS} / 共 {_PASS + _FAIL}")
    if _FAIL:
        print(f"  {_FAIL} 项失败 ❌")
        return 1
    print("  全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
