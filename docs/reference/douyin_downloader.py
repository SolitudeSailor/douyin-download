# -*- coding: utf-8 -*-
"""
抖音视频下载器（教学演示版）
==============================

目标：给定一个抖音分享链接 / 视频 ID，解析出无水印视频地址并下载到本地。

本文件刻意保持在「纯 Python 单文件 + 最少依赖」的水平，方便看懂整个链路：

    分享短链  ->  aweme_id  ->  详情接口  ->  取无水印 CDN 直链  ->  下载

注意（务必阅读）：
1. 抖音 Web 接口对请求做风控，核心门槛是 a_bogus / X-Bogus 签名和 ttwid / msToken 等令牌。
   本演示版**不实现签名算法**，走的是「浏览器里复制完整请求」的路线：
   你从 DevTools 复制一条真实请求的 cURL / Cookie，程序复用它的凭据。
   这是学习最快、最稳的方式；要工程化请使用开源库（见 README 的 f2 / TikTokDownloader）。
2. 仅用于个人学习与技术研究。请遵守抖音用户协议与《个人信息保护法》，
   不要用于批量抓取、商业分发或抓取他人隐私数据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, Optional

import requests

# ---------------------------------------------------------------------------
# 常量：抖音 Web 端的固定参数
# ---------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 移动端 UA —— 用于「分享短链跳转」环节，移动端页面更容易拿到 aweme_id
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# 详情接口的基础 query（不同时期参数会变，以你 DevTools 里抓到的为准）
BASE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "170400",
    "version_name": "17.4.0",
    "cookie_enabled": "true",
    "screen_width": "1920",
    "screen_height": "1080",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "125.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "os_name": "Windows",
    "os_version": "10",
    "platform": "PC",
}


# ---------------------------------------------------------------------------
# 第一步：把「分享文本 / 短链」规整成干净的 URL
# ---------------------------------------------------------------------------
def extract_url(text: str) -> str:
    """从分享文本中抽取 URL。

    抖音 App 的「复制链接」结果长这样：
        7.43 Xyz:/ 复制打开抖音，看看【作者的作品】... https://v.douyin.com/abcdEfg/
    所以要先正则捞出 http(s) 链接。
    """
    text = (text or "").strip()
    m = re.search(r"https?://[^\s\u4e00-\u9fff]+", text)
    if not m:
        raise ValueError(f"没能在文本里找到链接: {text!r}")
    return m.group(0)


# ---------------------------------------------------------------------------
# 第二步：短链 -> 长链 -> 提取 aweme_id
# ---------------------------------------------------------------------------
AWEME_ID_PATTERNS = [
    r"/video/(\d+)",          # https://www.douyin.com/video/7xxxxxxxxxx
    r"/note/(\d+)",           # 图文笔记
    r"modal_id=(\d+)",        # 部分分享链接走 modal_id
    r"aweme_id=(\d+)",
]


def resolve_aweme_id(url: str, session: requests.Session) -> str:
    """给定任意抖音链接，返回数字形式的 aweme_id。

    策略：
      1. 链接里已经带 id 就直接用；
      2. 否则跟随 302 跳转拿到最终长链，正则提取；
      3. 再不行，用移动端 UA 请求一次，从返回 HTML 里找 id。
    """
    # 策略 1：直接命中
    for pat in AWEME_ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)

    # 策略 2：跟随跳转
    try:
        resp = session.get(
            url,
            headers={"User-Agent": MOBILE_UA},
            allow_redirects=True,
            timeout=15,
        )
        final_url = resp.url
        for pat in AWEME_ID_PATTERNS:
            m = re.search(pat, final_url)
            if m:
                return m.group(1)
        # 策略 3：从 HTML 里挖
        m = re.search(r'"aweme_id"\s*:\s*"(\d+)"', resp.text)
        if m:
            return m.group(1)
        m = re.search(r'"itemId"\s*:\s*"(\d+)"', resp.text)
        if m:
            return m.group(1)
    except requests.RequestException as e:
        raise RuntimeError(f"跟随短链跳转失败: {e}") from e

    raise RuntimeError(f"无法从链接中解析出 aweme_id: {url}")


# ---------------------------------------------------------------------------
# 第三步：调详情接口，拿结构化数据
# ---------------------------------------------------------------------------
def fetch_aweme_detail(
    aweme_id: str,
    session: requests.Session,
    cookie: str = "",
) -> Dict[str, Any]:
    """请求视频详情接口。

    关键点：
      - 必须带 Referer（指向视频页），否则容易被拒；
      - 必须带完整 Cookie（含 ttwid / msToken / __ac_nonce 等）；
      - 签名参数（a_bogus / X-Bogus / _signature）本演示版不生成，
        如果服务端校验严格，请改用带签名的开源库。
    """
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://www.douyin.com/video/{aweme_id}",
        "Cookie": cookie,
    }
    params = dict(BASE_PARAMS)
    params["aweme_id"] = aweme_id

    resp = session.get(DETAIL_API, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status_code") != 0:
        raise RuntimeError(
            "接口返回错误 status_code="
            f"{data.get('status_code')} msg={data.get('status_msg')}；"
            "通常是 Cookie 过期或缺少签名参数。"
        )
    detail = data.get("aweme_detail")
    if not detail:
        raise RuntimeError("响应里没有 aweme_detail，可能触发了风控（空数据）。")
    return detail


# ---------------------------------------------------------------------------
# 第四步：从详情里挑出「无水印」视频地址
# ---------------------------------------------------------------------------
def pick_no_watermark_url(detail: Dict[str, Any]) -> str:
    """按码率/分辨率挑一个最优的无水印直链。

    抖音 Web 端返回的 play_addr 通常已经是无水印源；
    bit_rate 里含更多清晰度候选，按 bit_rate 从高到低取第一个可用的。
    """
    video = detail.get("video") or {}

    # 优先：bit_rate 列表，按码率降序
    bit_rates = video.get("bit_rate") or []
    candidates = []
    for br in bit_rates:
        url_list = (br.get("play_addr") or {}).get("url_list") or []
        if url_list:
            candidates.append((br.get("bit_rate", 0), url_list[0]))
    candidates.sort(key=lambda x: x[0], reverse=True)

    # 兜底：顶层 play_addr
    if not candidates:
        url_list = (video.get("play_addr") or {}).get("url_list") or []
        if url_list:
            candidates.append((0, url_list[0]))

    if not candidates:
        raise RuntimeError("详情里没有可用的视频播放地址。")
    return candidates[0][1]


def pick_play_urls(detail: Dict[str, Any]) -> list[str]:
    """返回所有候选播放地址（按优先级），便于某个 CDN 挂了时重试。"""
    video = detail.get("video") or {}
    urls: list[str] = []
    for br in video.get("bit_rate") or []:
        for u in (br.get("play_addr") or {}).get("url_list") or []:
            urls.append(u)
    for u in (video.get("play_addr") or {}).get("url_list") or []:
        urls.append(u)
    # 去重保序
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ---------------------------------------------------------------------------
# 第五步：下载
# ---------------------------------------------------------------------------
def download(urls: list[str], out_path: str, aweme_id: str) -> str:
    """按候选地址依次尝试下载。CDN 直链需要带 Referer 防盗链校验。"""
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
    }
    last_err: Optional[Exception] = None
    for url in urls:
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                done = 0
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = done * 100 // total
                            print(f"\r  下载中 {pct:3d}%  ({done/1e6:.1f}MB)", end="")
            print()
            if os.path.getsize(out_path) > 0:
                return out_path
        except Exception as e:  # noqa: BLE001  — 逐个候选重试，最后统一抛出
            last_err = e
            print(f"  该地址失败，尝试下一个: {e}")
            continue
    raise RuntimeError(f"所有候选地址都下载失败: {last_err}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(link: str, out_dir: str, cookie: str) -> str:
    session = requests.Session()

    print(f"[1/5] 解析链接: {link}")
    url = extract_url(link)

    print("[2/5] 提取 aweme_id ...")
    aweme_id = resolve_aweme_id(url, session)
    print(f"      aweme_id = {aweme_id}")

    print("[3/5] 请求详情接口 ...")
    detail = fetch_aweme_detail(aweme_id, session, cookie=cookie)

    desc = (detail.get("desc") or "无标题").replace("\n", " ")[:60]
    author = ((detail.get("author") or {}).get("nickname")) or "未知作者"
    print(f"      作者: {author}")
    print(f"      标题: {desc}")

    print("[4/5] 解析无水印地址 ...")
    urls = pick_play_urls(detail)
    if not urls:
        raise RuntimeError("没解析到播放地址。")
    print(f"      共 {len(urls)} 个候选地址")

    print("[5/5] 下载视频 ...")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{aweme_id}.mp4")
    download(urls, out_path, aweme_id)
    print(f"\n完成 -> {out_path}")

    # 顺手把元数据存一份，方便后续分析
    meta_path = os.path.join(out_dir, f"{aweme_id}.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音无水印视频下载器（教学版）")
    parser.add_argument("link", help="抖音分享链接 或 完整分享文本")
    parser.add_argument("-o", "--out", default="downloads", help="输出目录（默认 downloads）")
    parser.add_argument(
        "-c",
        "--cookie",
        default=os.environ.get("DOUYIN_COOKIE", ""),
        help="抖音 Cookie（也可用环境变量 DOUYIN_COOKIE）",
    )
    args = parser.parse_args()

    if not args.cookie:
        print(
            "!! 警告：没有提供 Cookie，详情接口大概率会被风控拦截。\n"
            "   请在浏览器登录抖音后，从 DevTools 复制整条 Cookie，\n"
            "   用 -c '...' 传入或设置环境变量 DOUYIN_COOKIE。\n",
            file=sys.stderr,
        )

    try:
        run(args.link, args.out, args.cookie)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"\n[失败] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
