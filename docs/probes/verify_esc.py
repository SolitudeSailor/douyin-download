# -*- coding: utf-8 -*-
"""V-3：验证「Esc 退出最大化」—— 桩测试四场景 + 真实按键。

前置：dist\\douyin-tool.exe 已经在窗口模式运行（端口从数据目录的 running.json 读）。

第一部分（桩测试）：用 agent-browser 打开 exe 真实提供的页面，把 window.api
换成记录桩，派发 KeyboardEvent，看四条边界是否都对，并断言从未触碰 /api/window/close。

第二部分（真实按键）：POST /api/window/maximize 真最大化 → POST /api/focus 置前 →
确认前台窗口确实是本程序 → keybd_event(VK_ESCAPE) → GET 应回到未最大化。

运行：
    python docs\\probes\\verify_esc.py
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from douyin_tool.config import get_data_dir           # noqa: E402

# running.json 的落点由 src/douyin_tool/config.py 统一决定（= 数据目录），不要硬编码 dist/ ——
# 打包好的 exe 旁边若存在 data_dir.txt，数据会落到别处，硬编码就找不到端口了。
RUNNING = get_data_dir() / "running.json"


def _agent_browser_command() -> list[str]:
    """优先使用显式配置，其次查找 PATH 和 npm 全局安装目录。"""
    explicit = os.environ.get("AGENT_BROWSER_JS")
    node = os.environ.get("NODE_EXE") or shutil.which("node")
    if explicit and node:
        return [node, explicit]

    cli = shutil.which("agent-browser")
    if cli:
        return [cli]

    npm = shutil.which("npm")
    if node and npm:
        result = subprocess.run(
            [npm, "root", "-g"], capture_output=True, text=True, check=False,
        )
        script = pathlib.Path(result.stdout.strip()) / "agent-browser" / "bin" / "agent-browser.js"
        if result.returncode == 0 and script.is_file():
            return [node, str(script)]
    return []


AB_COMMAND = _agent_browser_command()
APP_TITLE = "抖音视频下载器"
VK_ESCAPE = 0x1B

STUB_JS = r"""
(function(){
  window.__escResult = null;
  var allLog = [];
  var stubData = {maximized:false};
  window.api = function(path, opts){
    var method = (opts && opts.method) || 'GET';
    allLog.push(method + ' ' + path);
    return Promise.resolve({ok:true, status:200, data: stubData});
  };
  function hit(){
    var e = new KeyboardEvent('keydown', {key:'Escape', bubbles:true, cancelable:true});
    document.dispatchEvent(e);
    return e.defaultPrevented;
  }
  var out = {};
  function step1(cb){
    document.body.classList.add('win-maximized');
    stubData = {maximized:false};
    allLog.length = 0;
    out.s1_prevented = hit();
    setTimeout(function(){ out.s1_calls = allLog.slice(); cb(); }, 80);
  }
  function step2(cb){
    document.body.classList.remove('win-maximized');
    stubData = {maximized:false};
    allLog.length = 0;
    out.s2_prevented = hit();
    setTimeout(function(){ out.s2_calls = allLog.slice(); cb(); }, 80);
  }
  function step3(cb){
    document.body.classList.remove('win-maximized');
    stubData = {maximized:true};
    allLog.length = 0;
    out.s3_prevented = hit();
    setTimeout(function(){ out.s3_calls = allLog.slice(); cb(); }, 150);
  }
  function step4(cb){
    document.body.classList.add('win-maximized');
    var inp = document.querySelector('input, textarea');
    if (!inp){ out.s4_calls = ['SKIP: 页面里没有输入框']; return cb(); }
    inp.focus();
    stubData = {maximized:true};
    allLog.length = 0;
    out.s4_activeTag = document.activeElement.tagName;
    out.s4_prevented = hit();
    setTimeout(function(){ out.s4_calls = allLog.slice(); inp.blur(); cb(); }, 80);
  }
  step1(function(){ step2(function(){ step3(function(){ step4(function(){
    out.allCalls = allLog.slice();
    out.touchedClose = allLog.some(function(s){ return s.indexOf('/api/window/close') >= 0; });
    out.bodyClasses = document.body.className;
    window.__escResult = out;
  }); }); }); });
  return 'started';
})()
"""


def http(method: str, url: str, timeout: int = 8):
    req = urllib.request.Request(url, method=method)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_raw": body}


def read_port() -> int | None:
    if not RUNNING.is_file():
        return None
    try:
        return int(json.loads(RUNNING.read_text(encoding="utf-8"))["port"])
    except Exception:
        return None


def ab(args: list[str], timeout: int = 90) -> tuple[bool, str]:
    try:
        cp = subprocess.run([*AB_COMMAND, *args], capture_output=True,
                            timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    return cp.returncode == 0, (cp.stdout or "") + (cp.stderr or "")


def unwrap(s: str):
    """agent-browser 会把返回的字符串再按 JSON 编码一层，循环解到不是字符串为止。"""
    v = s.strip()
    for _ in range(4):
        try:
            v2 = json.loads(v)
        except Exception:
            break
        if isinstance(v2, str):
            v = v2.strip()
            continue
        return v2
    return v


def foreground_title() -> tuple[int, str]:
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    hwnd = u.GetForegroundWindow() or 0
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return hwnd, buf.value


def find_hwnd(title: str) -> int:
    u = ctypes.windll.user32
    u.FindWindowW.restype = ctypes.c_void_p
    return int(u.FindWindowW(None, title) or 0)


def force_foreground(hwnd: int) -> bool:
    """把窗口弄到前台。

    Windows 有「前台锁定」：非前台进程直接调 SetForegroundWindow 会被忽略。
    标准绕法是先把当前线程的输入队列 attach 到前台窗口线程与目标窗口线程，
    再 SetForegroundWindow，最后 detach。
    """
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    if (u.GetForegroundWindow() or 0) == hwnd:
        return True
    cur = k.GetCurrentThreadId()
    tids = {u.GetWindowThreadProcessId(u.GetForegroundWindow() or 0, None),
            u.GetWindowThreadProcessId(hwnd, None)}
    attached = []
    for tid in tids:
        if tid and tid != cur and u.AttachThreadInput(cur, tid, True):
            attached.append(tid)
    try:
        u.ShowWindow(hwnd, 9)          # SW_RESTORE
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            u.AttachThreadInput(cur, tid, False)
    for _ in range(12):
        if (u.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.15)
    return False


def main() -> int:
    port = read_port()
    if not port:
        print("[错误] 没找到 dist\\running.json 或里面没有 port —— 请先启动 exe")
        return 2
    base = f"http://127.0.0.1:{port}"
    print("=" * 74)
    print(f"  V-3 Esc 验证   (端口 {port})")
    print("=" * 74)

    if not AB_COMMAND:
        print("[错误] 找不到 agent-browser；请加入 PATH，或设置 NODE_EXE 和 AGENT_BROWSER_JS。")
        return 2

    ok = True

    # ---------------- 第一部分：桩测试 ----------------
    print("\n[1/2] 桩测试（window.api 换记录桩 + 派发 KeyboardEvent）")
    r_ok, out = ab(["open", base + "/"])
    print(f"   open -> {'ok' if r_ok else '失败'}")
    if not r_ok:
        print("   " + out.strip()[:300])
        return 2
    time.sleep(1.2)

    r_ok, out = ab(["eval", STUB_JS])
    print(f"   eval(启动测试) -> {out.strip()[:120]}")
    time.sleep(1.6)

    r_ok, out = ab(["eval", "JSON.stringify(window.__escResult)"])
    res = unwrap(out)
    if not isinstance(res, dict):
        print(f"   x 读不到测试结果，原始输出：{out.strip()[:300]}")
        return 2

    def show(name, cond, detail):
        nonlocal ok
        if cond:
            print(f"   v {name}: {detail}")
        else:
            ok = False
            print(f"   x {name}: {detail}")

    s1 = res.get("s1_calls") or []
    show("场景1 已最大化",
         any(c.startswith("POST /api/window/maximize") for c in s1) and res.get("s1_prevented") is True,
         f"prevented={res.get('s1_prevented')} calls={s1}")

    s2 = res.get("s2_calls") or []
    show("场景2 未最大化且后端报 false",
         len(s2) >= 1 and all(c.startswith("GET ") for c in s2),
         f"calls={s2}（应只有 GET，无 POST）")

    s3 = res.get("s3_calls") or []
    show("场景3 状态类过期（后端报 true）",
         any(c.startswith("GET ") for c in s3) and any(c.startswith("POST ") for c in s3),
         f"calls={s3}（应 GET 求证后再补 POST）")

    s4 = res.get("s4_calls") or []
    show("场景4 焦点在输入框里",
         s4 == [] or (len(s4) == 1 and str(s4[0]).startswith("SKIP")),
         f"activeTag={res.get('s4_activeTag')} calls={s4}（应零调用）")

    show("全程未触碰关闭接口", res.get("touchedClose") is False,
         f"touchedClose={res.get('touchedClose')}")

    # ---------------- 第二部分：真实按键 ----------------
    print("\n[2/2] 真实按键（真窗口 + 真键盘事件）")
    st = http("POST", base + "/api/window/maximize")
    time.sleep(0.8)
    cur = http("GET", base + "/api/window/maximize")
    if cur.get("maximized") is not True:          # 上一轮可能已留在最大化态，toggle 会把它还原
        st = http("POST", base + "/api/window/maximize")
        time.sleep(0.8)
        cur = http("GET", base + "/api/window/maximize")
    print(f"   POST 最大化 -> {st}；GET 复查 -> {cur}")
    if cur.get("maximized") is not True:
        ok = False
        print("   x 没能把窗口切到最大化，真实按键部分无法继续")
    else:
        http("POST", base + "/api/focus")
        time.sleep(0.6)
        hwnd = find_hwnd(APP_TITLE)
        print(f"   目标窗口 hwnd={hwnd}")
        if not hwnd:
            ok = False
            print("   x 找不到本程序窗口")
        else:
            got = force_foreground(hwnd)
            cur_hwnd, title = foreground_title()
            print(f"   置前={got}  当前前台 hwnd={cur_hwnd} title={title!r}")
            if not got:
                ok = False
                print("   ! 无法把窗口置前 —— 跳过真实按键（避免把 Esc 发给别的窗口）")
            else:
                u = ctypes.windll.user32
                u.keybd_event(VK_ESCAPE, 0, 0, 0)
                time.sleep(0.08)
                u.keybd_event(VK_ESCAPE, 0, 2, 0)
                time.sleep(1.5)
                after = http("GET", base + "/api/window/maximize")
                print(f"   发键后 GET -> {after}")
                if after.get("maximized") is False:
                    print("   v 真实按 Esc 后窗口已从最大化还原")
                else:
                    ok = False
                    print("   x 真实按 Esc 后仍未还原")

    print("\n   结论：" + ("v 全部通过" if ok else "x 有项目未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
