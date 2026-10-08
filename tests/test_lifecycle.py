# -*- coding: utf-8 -*-
"""生命周期测试：关掉页面之后进程必须真的退出。

用法：
    python tests/test_lifecycle.py

为什么必须起子进程：
    这条逻辑的产物就是「进程消失了」，在进程内断言不出来 ——
    只能真的拉起一个服务进程，再从外面观察它有没有退出。

覆盖三种情形（全程约 40 秒）：
    A. 只发心跳、不发关闭 → 必须活着（别把正在正常使用的实例误杀）
    B. 先上报关闭、再发心跳 → 必须活着（刷新页面不能被当成关闭）
    C. 上报关闭后不再心跳 → 必须在宽限期后退出，退出原因写进 logs/app.log
    D. 心跳超时了但进程被挂起过（合盖休眠）→ 必须活着，不能自杀

D 的实现办法：起一个子进程，把 IDLE_TIMEOUT 压到 1.5 秒、把 SUSPEND_GAP 压到
0.3 秒。看门狗每 1 秒 tick 一次，于是每次 gap(≈1.0) 都大于 SUSPEND_GAP，
「挂起」分支每次都会把心跳基准推到当前 —— 心跳超时永远不成立，进程必须活着。
没有那段防休眠逻辑的话，它会在 1.5 秒后自己退出，这条用例就会失败。

用 `--no-browser` 启动，因此不会弹浏览器窗口，也不会触发单实例守卫。
自动退出由环境变量 DYD_AUTO_EXIT=1 强制打开（--no-browser 默认是关的）。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))
from support import isolate_data

isolate_data()

# 本文件是「自带断言的独立脚本」，正确跑法是 `python tests/test_lifecycle.py`。
# __test__=False 阻止 pytest 误收集（函数名恰好是 test_*，但签名不是 pytest 风格）。
__test__ = False

PORT = int(os.environ.get("TEST_LIFECYCLE_PORT", "8821"))
BASE = f"http://127.0.0.1:{PORT}"

# 用例 D 用：把超时压到秒级，并让每次 tick 都落进「进程被挂起过」的分支。
# 常量是模块级全局，run_server 看门狗启动后读的就是这里改过的值。
_SUSPEND_PORT = int(os.environ.get("TEST_LIFECYCLE_SUSPEND_PORT", "8822"))
_SUSPEND_CHILD_CODE = (
    "import sys; sys.path.insert(0, 'src');"
    "from douyin_tool import web_app as w;"
    "w.IDLE_TIMEOUT = 1.5;"
    "w.SUSPEND_GAP = 0.3;"
    f"w.run_server(host='127.0.0.1', port={_SUSPEND_PORT}, open_browser=False)"
)

_PASS = 0
_FAIL = 0


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"[PASS] {name}  {detail}")
    else:
        _FAIL += 1
        print(f"[FAIL] {name}  {detail}")


def post(path, timeout=5, base=None):
    req = urllib.request.Request((base or BASE) + path, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception as e:  # noqa: BLE001
        return None


def get_json(path, timeout=5, base=None):
    try:
        with urllib.request.urlopen((base or BASE) + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def wait_ready(timeout=40.0, base=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_json("/api/alive", timeout=2, base=base):
            return True
        time.sleep(0.4)
    return False


def log_file() -> Path:
    """和 console._log_path 保持一致：开发态 = <项目根>/data/logs/app.log。"""
    try:
        from douyin_tool import config as cfg_mod
        return cfg_mod.get_data_dir() / "logs" / "app.log"
    except Exception:  # noqa: BLE001
        return BASE_DIR / "data" / "logs" / "app.log"


def tail_lines(p: Path) -> list:
    try:
        return p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def main() -> int:
    print("=" * 66)
    print("  生命周期测试：关掉页面 → 进程退出")
    print("=" * 66)
    print(f"  端口 {PORT}，脚本 {sys.executable}\n")

    env = dict(os.environ)
    env["DYD_AUTO_EXIT"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [sys.executable, "-m", "douyin_tool", "--no-browser", "--port", str(PORT)],
        cwd=str(BASE_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        # ---- 启动 ----
        if not wait_ready():
            check("服务能起来", False, f"进程码={proc.poll()}，40 秒内没就绪")
            return 1
        check("服务能起来", True, f"pid={proc.pid}")

        alive = get_json("/api/alive") or {}
        check("身份探针返回本程序标记", alive.get("app") == "douyin-tool",
              f"app={alive.get('app')!r} pid={alive.get('pid')}")

        # ---- A. 只心跳，不关闭 → 必须活着 ----
        post("/api/heartbeat")
        time.sleep(6.5)          # > CLOSE_GRACE(4)
        check("A 只发心跳时不会退出", proc.poll() is None,
              f"退出码={proc.poll()}")

        # ---- B. 先报关闭、再心跳（模拟刷新）→ 必须活着 ----
        post("/api/closing")
        time.sleep(1.5)
        post("/api/heartbeat")   # 新页面连上 = 取消待退出
        time.sleep(6.5)
        check("B 刷新页面不会被误判成关闭", proc.poll() is None,
              f"退出码={proc.poll()}")

        # ---- E. 单实例守卫：能发现活着的实例，且不引向正在退出的实例 ----
        # 不另起浏览器（守卫只在 open_browser 模式下拦人），直接调内部函数，
        # 对着上面这个真实在跑的实例验证。
        from douyin_tool import web_app as w
        check("E 能发现正在运行的实例", w._existing_instance() == PORT,
              f"期望端口 {PORT}，实际 {w._existing_instance()}")
        post("/api/closing")
        time.sleep(0.3)
        check("E 不去引向「正在退出」的实例", w._existing_instance() is None,
              "已上报关闭的实例应当被视为不可用")

        # ---- C. 报关闭且不再心跳 → 必须退出，且原因落盘 ----
        before = len(tail_lines(log_file()))
        deadline = time.time() + 20
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.4)
        code = proc.poll()
        check("C 上报关闭后进程退出", code is not None,
              f"退出码={code}（等了 {20 - int(deadline - time.time())} 秒）")
        check("C 退出码为 0", code == 0, f"实际={code}")

        new_lines = tail_lines(log_file())[before:]
        joined = "\n".join(new_lines)
        check("C 退出原因写进了 logs/app.log", "页面已关闭" in joined,
              f"新增日志 {len(new_lines)} 行")

        # ---- 实例记录要被清干净（否则下次启动会读到脏数据）----
        running = None
        try:
            from douyin_tool import config as cfg_mod
            running = cfg_mod.get_data_dir() / "running.json"
        except Exception:  # noqa: BLE001
            running = BASE_DIR / "data" / "running.json"
        check("退出后清理 running.json", not running.exists(),
              f"{running}")

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    # ---- D. 心跳超时 + 进程被挂起过（模拟合盖休眠）→ 必须活着 ----
    dbase = f"http://127.0.0.1:{_SUSPEND_PORT}"
    proc2 = subprocess.Popen(
        [sys.executable, "-c", _SUSPEND_CHILD_CODE],
        cwd=str(BASE_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(40, base=dbase):
            check("D 服务能起来", False, f"进程码={proc2.poll()}")
        else:
            post("/api/heartbeat", base=dbase)   # 先让 seen 成立
            time.sleep(8)                        # 远超被压到 1.5 秒的 IDLE_TIMEOUT
            check("D 挂起醒来后不会把自己杀掉", proc2.poll() is None,
                  f"退出码={proc2.poll()}（IDLE_TIMEOUT=1.5s 已过 8s）")
            post("/api/closing", base=dbase)
            deadline = time.time() + 20
            while time.time() < deadline and proc2.poll() is None:
                time.sleep(0.4)
            check("D 关闭上报依然生效（不是把看门狗整废了）",
                  proc2.poll() is not None, f"退出码={proc2.poll()}")
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=10)

    print()
    print("=" * 66)
    print(f"  结果：{_PASS} 通过，{_FAIL} 失败")
    print("=" * 66)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
