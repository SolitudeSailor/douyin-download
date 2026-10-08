# -*- coding: utf-8 -*-
"""链接解析回归测试 —— 纯离线，不联网、不下载。

用法：
    python tests/test_link_parse.py

背景（这是一次真实故障的回归测试）：
    用户粘贴的是抖音「精选」页里点开作品后的地址
        https://www.douyin.com/jingxuan?modal_id=7682373106140319026
    这类链接把作品 ID 放在 **查询参数 modal_id** 里，跳转后路径仍是
    /jingxuan。f2 的 AwemeIdFetcher 只在跳转后 URL 里匹配
        video/([^/?]*)  或  note/([^/?]*)
    于是匹配失败，抛「未在响应的地址中找到 aweme_id」，
    被包装成「无法从链接中解析出作品 ID」。

    修复方式：core 自己先用正则把 ID 从各种形态里抠出来（不联网），
    只有 v.douyin.com 短链这种必须跟随跳转的才交给 f2。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from douyin_tool import core  # noqa: E402
from douyin_tool.web_app import _check_url  # noqa: E402

# 本文件是「自带断言的独立脚本」，正确跑法是 `python tests/test_link_parse.py`。
# __test__=False 阻止 pytest 误收集（函数名恰好是 test_*，但签名不是 pytest 风格）。
__test__ = False

AID = "7682373106140319026"
OTHER_AID = "7689062596959358235"
SEC = "MS4wLjABAAAAbcdefghijklmnop1234567890"

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


# ---------------------------------------------------------------------------
# extract_aweme_id：作品 ID 的各种来源形态
# ---------------------------------------------------------------------------
AWEME_CASES = [
    ("纯数字 ID", AID, AID),
    ("标准作品页 /video/", f"https://www.douyin.com/video/{AID}", AID),
    ("图文笔记页 /note/", f"https://www.douyin.com/note/{AID}", AID),
    ("精选弹窗 ?modal_id= ★故障原形", f"https://www.douyin.com/jingxuan?modal_id={AID}", AID),
    ("发现页 ?modal_id=", f"https://www.douyin.com/discover?modal_id={AID}&a=1", AID),
    ("搜索页 ?modal_id=", f"https://www.douyin.com/search/xxx?modal_id={AID}", AID),
    ("主页内点开作品（应取作品而非主页）", f"https://www.douyin.com/user/{SEC}?modal_id={AID}", AID),
    ("iesdouyin 分享页", f"https://www.iesdouyin.com/share/video/{AID}/?region=CN", AID),
    ("m.douyin 分享页", f"https://m.douyin.com/share/video/{AID}", AID),
    ("?aweme_id= 参数", f"https://www.douyin.com/x?aweme_id={AID}", AID),
    ("?item_id= 参数", f"https://www.douyin.com/x?item_id={AID}", AID),
    ("?vid= 参数", f"https://www.douyin.com/x?vid={AID}", AID),
    ("整个分享文本", f"7.43 复制打开抖音，看看【某某】的作品 https://www.douyin.com/video/{AID} 复制此链接", AID),
    ("modal_id 值非数字 → 不误判", "https://www.douyin.com/jingxuan?modal_id=abc", None),
    ("纯主页链接 → 不是作品", f"https://www.douyin.com/user/{SEC}", None),
    ("v.douyin.com 短链 → 交给联网解析", "https://v.douyin.com/iRNBho6u/", None),
    ("空串", "", None),
]


def test_extract_aweme_id():
    for name, raw, want in AWEME_CASES:
        check(name, core.extract_aweme_id(raw), want)


# ---------------------------------------------------------------------------
# extract_sec_user_id：用户主页 ID
# ---------------------------------------------------------------------------
SEC_CASES = [
    ("纯 sec_user_id", SEC, SEC),
    ("主页 URL", f"https://www.douyin.com/user/{SEC}?from_tab_name=main", SEC),
    ("主页 URL + modal_id", f"https://www.douyin.com/user/{SEC}?modal_id={AID}", SEC),
    ("作品页 → 无用户 ID", f"https://www.douyin.com/video/{AID}", None),
]


def test_extract_sec_user_id():
    for name, raw, want in SEC_CASES:
        check(name, core.extract_sec_user_id(raw), want)


# ---------------------------------------------------------------------------
# _check_url：域名白名单（安全）——不能因为放宽子域而放过伪装域名
# ---------------------------------------------------------------------------
URL_CASES = [
    ("www 作品页", f"https://www.douyin.com/video/{AID}", True),
    ("jingxuan 精选", f"https://www.douyin.com/jingxuan?modal_id={AID}", True),
    ("m 站", f"https://m.douyin.com/share/video/{AID}", True),
    ("v 短链", "https://v.douyin.com/iRNBho6u/", True),
    ("iesdouyin", f"https://www.iesdouyin.com/share/video/{AID}/", True),
    ("伪装域名 douyin.com.evil.com", f"https://www.douyin.com.evil.com/video/{AID}", False),
    ("外部域名", f"https://www.evil.com/video/{AID}", False),
    ("相似域名 evildouyin.com", f"https://evildouyin.com/video/{AID}", False),
]


def test_check_url():
    for name, raw, allowed in URL_CASES:
        err = _check_url(raw)
        check(name + ("（放行）" if allowed else "（拦截）"),
              err is None, allowed)


# ---------------------------------------------------------------------------
# normalize_douyin_url：抹掉链接后面那些没用的参数
# ---------------------------------------------------------------------------
NORMALIZE_CASES = [
    ("搜索页 + &type=general ★用户报的",
     f"https://www.douyin.com/search/%E7%94%B5%E8%84%91%E6%B8%85%E7%81%B0?modal_id={AID}&type=general",
     f"https://www.douyin.com/video/{AID}"),
    ("搜索页（未编码中文）",
     f"https://www.douyin.com/search/电脑清灰?modal_id={AID}&type=general",
     f"https://www.douyin.com/video/{AID}"),
    ("精选弹窗",
     f"https://www.douyin.com/jingxuan?modal_id={AID}",
     f"https://www.douyin.com/video/{AID}"),
    ("主页内点开作品 → 应得作品，不降级成主页",
     f"https://www.douyin.com/user/{SEC}?modal_id={AID}",
     f"https://www.douyin.com/video/{AID}"),
    ("标准作品页（query 杂参抹掉）",
     f"https://www.douyin.com/video/{AID}?previous_page=web_code_link",
     f"https://www.douyin.com/video/{AID}"),
    ("纯主页 → 用户主页",
     f"https://www.douyin.com/user/{SEC}?from_tab_name=main",
     f"https://www.douyin.com/user/{SEC}"),
    ("纯数字 ID 原样",
     AID, AID),
    ("分享文本里抽出并规范化",
     f"7.43 复制打开抖音，看看【某某】的作品 https://www.douyin.com/video/{AID} 复制此链接",
     f"https://www.douyin.com/video/{AID}"),
    ("短链必须原样保留（需联网跟随跳转）",
     "https://v.douyin.com/iRNBho6u/",
     "https://v.douyin.com/iRNBho6u/"),
    ("空串", "", ""),
]


def test_normalize_url():
    for name, raw, want in NORMALIZE_CASES:
        check(name, core.normalize_douyin_url(raw), want)


# ---------------------------------------------------------------------------
# _wrap_error：报错文案不能被无关异常误触发
# ---------------------------------------------------------------------------
def test_error_attribution():
    from douyin_tool.core import DouyinError, LinkParseError

    # 下游无关异常提到 aweme_id，不能再被翻译成「无法从链接中解析出作品 ID」
    got = str(core._wrap_error(KeyError("aweme_id")))
    check("KeyError('aweme_id') 不再误报解析失败",
          "无法从链接中解析出作品 ID" in got, False)

    # 解析失败只能由 LinkParseError 承担，且原样透传（不被二次翻译）
    parse_msg = "无法从链接中解析出作品 ID，请确认链接是否正确、是否为作品页。"
    wrapped = core._wrap_error(LinkParseError(parse_msg))
    check("LinkParseError 原样透传", str(wrapped), parse_msg)
    check("LinkParseError 是 DouyinError 子类",
          isinstance(wrapped, DouyinError), True)


def main():
    print("=" * 66)
    print("链接解析回归测试")
    print("=" * 66)
    print("\n[1] extract_aweme_id —— 作品 ID 来源形态")
    test_extract_aweme_id()
    print("\n[2] extract_sec_user_id —— 用户主页 ID")
    test_extract_sec_user_id()
    print("\n[3] _check_url —— 域名白名单")
    test_check_url()
    print("\n[4] normalize_douyin_url —— 抹掉无关参数")
    test_normalize_url()
    print("\n[5] _wrap_error —— 报错归因")
    test_error_attribution()

    print("\n" + "=" * 66)
    total = _PASS + _FAIL
    print(f"结果：{_PASS}/{total} 通过" + (f"，{_FAIL} 失败" if _FAIL else "，全绿 ✅"))
    print("=" * 66)

    if _FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
