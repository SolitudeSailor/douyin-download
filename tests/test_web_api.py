# -*- coding: utf-8 -*-
"""Web API 测试 —— 自起服务，独立可跑。

用法：
    python tests/test_web_api.py

说明：
- 早期版本硬编码 8792 端口、依赖外部先启动服务，跑起来会把「连不上」
  误报成测试失败。现改为脚本内自己起 Flask。
- 测试会临时清空 cookie 并在结束时**还原原配置**，不会破坏你的登录态。
- 不会调用 /api/login/forget（那会删掉真实登录 profile）。
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# 本文件是「自带断言的独立脚本」，正确跑法是 `python tests/test_web_api.py`。
# __test__=False 阻止 pytest 误收集（函数名恰好是 test_*，但签名不是 pytest 风格）。
__test__ = False

PORT = int(os.environ.get("TEST_PORT", "8815"))
BASE = f"http://127.0.0.1:{PORT}"

_PASS = 0
_FAIL = 0


def call(path, data=None, method=None):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method or ("POST" if data is not None else "GET")
    )
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return None, {"_err": str(e)}


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"[PASS] {name}  {detail}")
    else:
        _FAIL += 1
        print(f"[FAIL] {name}  {detail}")


def main():
    from support import isolate_data
    isolate_data()
    from douyin_tool import config as cfg_mod
    from douyin_tool.web_app import app

    # 备份原配置，结束时还原
    backup = dict(cfg_mod.load_config())
    cfg_mod.update_config(cookie="", cookie_updated_at=0)

    threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1", port=PORT, debug=False, use_reloader=False
        ),
        daemon=True,
    ).start()
    time.sleep(2.0)

    print("=" * 60)
    print(f"Web API 测试（{BASE}）")
    print("=" * 60)

    try:
        # 1. 配置读取
        s, d = call("/api/config")
        check("配置读取", s == 200 and "download_dir" in d,
              f"HTTP {s} browser={d.get('browser')}")

        # 2. 登录状态接口
        s, d = call("/api/login/status")
        check("登录状态接口", s == 200 and d.get("ok") and "status" in d,
              f"HTTP {s} status={d.get('status')}")

        # 3. 浏览器探测（本机应能探到 Chrome 或 Edge）
        s, d = call("/api/config")
        check("浏览器探测返回文本", isinstance(d.get("browser"), str), f"browser={d.get('browser')}")

        # 4. 未登录时下载应被拦截并引导登录
        s, d = call("/api/download", {"url": "https://www.douyin.com/video/7298145681699622182", "mode": "one"})
        check("未登录拦截下载", s == 400 and d.get("need_login") is True,
              f"HTTP {s} msg={d.get('message')}")

        # 5. 登录取消接口
        s, d = call("/api/login/cancel", {})
        check("登录取消接口", s == 200 and d.get("ok") is True, f"HTTP {s}")

        # 6-8. SSRF 三层防护
        for label, url in [
            ("SSRF 恶意域名", "https://evil.example.com/steal"),
            ("SSRF 内网地址", "http://127.0.0.1:8080/admin"),
            ("SSRF 伪装域名", "https://douyin.com.evil.com/x"),
        ]:
            s, d = call("/api/download", {"url": url, "mode": "one"})
            check(label, s == 400, f"HTTP {s}")

        # 9. 正常链接解析（走本地正则，无需登录）
        s, d = call("/api/parse", {"url": "https://www.douyin.com/video/7298145681699622182"})
        check("正常抖音链接解析",
              s == 200 and d.get("ok") and d.get("info", {}).get("kind") == "video",
              f"HTTP {s} info={d.get('info')}")

        # 9b. 精选页 modal_id 链接（回归：曾报「无法从链接中解析出作品 ID」）
        s, d = call("/api/parse", {"url": "https://www.douyin.com/jingxuan?modal_id=7682373106140319026"})
        check("modal_id 链接解析",
              s == 200 and d.get("info", {}).get("id") == "7682373106140319026",
              f"HTTP {s} info={d.get('info')}")

        # 10. 任务列表
        s, d = call("/api/tasks")
        check("任务列表", s == 200 and d.get("ok") is True, f"HTTP {s}")

        # 11. 不存在的任务
        s, d = call("/api/progress/nonexistent")
        check("不存在任务返回 404", s == 404, f"HTTP {s}")

        # 12. 旧 cURL 接口应已下线
        s, d = call("/api/cookie/fetch-browser", {"browser": "chrome"})
        check("旧 cURL 接口已移除", s == 404, f"HTTP {s}")
    finally:
        cfg_mod.save_config(backup)

    print("=" * 60)
    print(f"结果：{_PASS} 通过 / {_FAIL} 失败")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.exit(code)
