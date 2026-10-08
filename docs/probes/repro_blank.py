# -*- coding: utf-8 -*-
"""复现「打开 exe 后窗口一片空白」—— 连续开关多次，每轮抓图判定。

判定口径（与用户截图的取证一致）：
    窗口内容区若 100% 是 #1F1E1B（深色主题的背景色 / 窗口首帧底色），
    连顶栏和卡片都没有 → 判为「白屏」（页面没渲染）。
    正常态里品牌色 #D97757 必须出现（主按钮），且颜色数上千。

用法：
    python docs\\probes\\repro_blank.py [轮数]      # 默认 3 轮
输出：控制台表格 + %TEMP%\\dyd_v2\\repro_roundN.png
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import grab_buttons as gb  # noqa: E402
from douyin_tool.config import get_data_dir  # noqa: E402

u = gb.u
wintypes = ctypes.wintypes  # noqa: E402

RUNNING = get_data_dir() / "running.json"
EXE = ROOT / "dist" / "douyin-tool.exe"
BG = (0x1F, 0x1E, 0x1B)
BRAND = (0xD9, 0x77, 0x57)
OUTDIR = pathlib.Path(os.environ.get("TEMP", ".")) / "dyd_v2"

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def all_hwnds(title: str) -> list[int]:
    """枚举所有标题匹配的顶层窗口（多实例时会有多个同名窗口）。"""
    found: list[int] = []

    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title and u.IsWindowVisible(hwnd):
                found.append(int(hwnd))
        return True

    u.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def http_post(url: str, timeout: int = 5):
    req = urllib.request.Request(url, method="POST")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe(hwnd: int, w: int = 620, h: int = 220) -> dict:
    """抓窗口左上角一块，判定是否有真实内容。"""
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
    r = gb.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    W, H = r.right - r.left, r.bottom - r.top
    if W <= 0 or H <= 0:
        return {"ok": False, "why": "窗口尺寸无效"}
    w, h = min(w, W), min(h, H)
    gb.force_foreground(hwnd)
    time.sleep(0.35)
    rgb = gb.grab(r.left, r.top, w, h)
    px = list(zip(rgb[0::3], rgb[1::3], rgb[2::3]))
    cnt = Counter(px)
    n = len(cnt)
    brand = any(abs(c[0] - BRAND[0]) < 16 and abs(c[1] - BRAND[1]) < 16
                and abs(c[2] - BRAND[2]) < 16 for c in px)
    top_bg = cnt.get(BG, 0) * 100.0 / len(px)
    return {"ok": brand and n > 50, "colors": n, "brand": brand,
            "bg_pct": round(top_bg, 1), "size": (W, H),
            "rect": (r.left, r.top), "blank": (n <= 3 and top_bg > 99)}


def shot(hwnd: int, name: str) -> pathlib.Path:
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(gb.RECT)]
    r = gb.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    W, H = r.right - r.left, r.bottom - r.top
    rgb = gb.grab(r.left, r.top, W, H)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / name
    gb.write_png(out, W, H, rgb)
    return out


def wait_ready(t0: float, timeout: float = 60.0):
    """等 running.json 出现，返回 (port, 耗时)。"""
    while time.time() - t0 < timeout:
        try:
            info = json.loads(RUNNING.read_text(encoding="utf-8"))
            return int(info["port"]), round(time.time() - t0, 2)
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    return None, None


def wait_gone(timeout: float = 45.0) -> float:
    t = time.time()
    while time.time() - t < timeout:
        if not RUNNING.exists():
            return round(time.time() - t, 2)
        try:
            info = json.loads(RUNNING.read_text(encoding="utf-8"))
            if not gb.http("GET", f"http://127.0.0.1:{info['port']}/api/alive", timeout=1):
                break
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.2)
    return round(time.time() - t, 2)


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print(f"=== 连续开关复现实验：{rounds} 轮 ===")
    print(f"exe: {EXE}  ({EXE.stat().st_size} 字节, "
          f"{time.strftime('%H:%M:%S', time.localtime(EXE.stat().st_mtime))})\n")

    # 先把已经开着的收干净
    for hw in all_hwnds(gb.APP_TITLE):
        print(f"  关闭已有窗口 hwnd={hw}")
    try:
        info = json.loads(RUNNING.read_text(encoding="utf-8"))
        http_post(f"http://127.0.0.1:{info['port']}/api/window/close")
    except Exception:  # noqa: BLE001
        pass
    if RUNNING.exists():
        print(f"  等旧实例退出… {wait_gone()}s")

    blanks = 0
    for i in range(1, rounds + 1):
        print(f"\n----- 第 {i} 轮 -----")
        t0 = time.time()
        p = subprocess.Popen([str(EXE)])
        port, t_port = wait_ready(t0)
        print(f"  [{t_port}s] running.json → 端口 {port}（引导 pid={p.pid}）")
        if not port:
            print("  × 服务没起来，跳过")
            continue

        hw = []
        while time.time() - t0 < 40:
            hw = all_hwnds(gb.APP_TITLE)
            if hw:
                break
            time.sleep(0.15)
        print(f"  [{round(time.time() - t0, 2)}s] 窗口 {hw}")
        if not hw:
            print("  × 窗口没出现")
            continue

        time.sleep(2.5)                     # 等页面画完
        r = probe(hw[0])
        tag = "★白屏" if r.get("blank") else ("正常" if r["ok"] else "可疑")
        print(f"  抓图判定：{tag}  颜色数={r.get('colors')} 品牌色={r.get('brand')} "
              f"背景占比={r.get('bg_pct')}% 窗口={r.get('size')}")
        out = shot(hw[0], f"repro_round{i}.png")
        print(f"  截图 → {out}")
        if not r["ok"]:
            blanks += 1

        # 关掉，然后立刻进下一轮（模拟"关了马上再开"）
        try:
            http_post(f"http://127.0.0.1:{port}/api/window/close")
        except Exception as e:  # noqa: BLE001
            print(f"  关闭请求失败：{e}")
        gone = wait_gone()
        print(f"  旧实例退出耗时 {gone}s（>8s 说明还在清 profile）")

    print(f"\n=== 结果：{rounds} 轮里 {blanks} 轮没渲染出内容 ===")
    return 0 if blanks == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
