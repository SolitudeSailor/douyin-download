# -*- coding: utf-8 -*-
"""冻结态（打包后的 exe）生命周期验证：打包产物是不是也「关页面就退干净」。

用法：
    python tests/test_frozen_exe.py

需要先打包：`python -m PyInstaller build/douyin_tool.spec --clean --noconfirm
--workpath build_tmp --distpath dist`

为什么要单独验一遍：开发态是「一个 python 进程」，exe 是 PyInstaller onefile 的
**两个进程**（引导父进程 + 真正干活的子进程）。子进程用 os._exit(0) 退场后，
父进程会不会跟着退、临时目录会不会被清掉 —— 这些只有跑真 exe 才知道。

不放进 run_all.py：它依赖先打包，且 exe 过期时会误报失败。
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))
from support import isolate_data

isolate_data()

__test__ = False

EXE = Path(os.environ.get("TEST_FROZEN_EXE", BASE_DIR / "dist" / "douyin-tool.exe")).resolve()
PORT = int(os.environ.get("TEST_FROZEN_PORT", "8841"))
IMG = "douyin-tool.exe"

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


def proc_count() -> int:
    """用系统进程快照计数；不能把 tasklist 被拒绝访问误判为零进程。"""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    for name in ("Process32FirstW", "Process32NextW"):
        func = getattr(kernel, name)
        func.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        func.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return -1
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        count = 0
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not valid:
            return -1
        while valid:
            if entry.szExeFile.lower() == IMG:
                count += 1
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return count
    finally:
        kernel.CloseHandle(snapshot)


def probe():
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/api/alive", timeout=3
        ) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def mei_dirs() -> set:
    """TEMP 下的 PyInstaller 解压目录。

    注意：只能比较「跑之前 / 跑之后」的差集 —— TEMP 里可能残留着以前被强杀
    的实例留下的 _MEI*，直接断言「一个都没有」会把历史垃圾算到本次头上。
    """
    tmp = Path(os.environ.get("TEMP", "."))
    return {p.name for p in tmp.glob("_MEI*")}


def main() -> int:
    print("=" * 66)
    print("  冻结态验证：打包后的 exe 关掉页面能不能退干净")
    print("=" * 66)

    if not EXE.is_file():
        print(f"\n  [跳过] 没有找到 {EXE}")
        print("         先打包一次再跑：python -m PyInstaller build/douyin_tool.spec "
              "--clean --noconfirm --workpath build_tmp --distpath dist")
        return 0

    print(f"  程序 {EXE}")
    print(f"  大小 {EXE.stat().st_size:,} 字节")
    print(f"  端口 {PORT}\n")

    env = dict(os.environ)
    env["DYD_AUTO_EXIT"] = "1"      # --no-browser 默认关掉自动退出，这里强制打开

    before = proc_count()
    check("开始前没有残留进程", before == 0, f"实际 {before} 个")
    if before != 0:
        print("已有程序正在运行，本次测试停止，避免影响现有实例。")
        return 1
    mei_before = mei_dirs()

    proc = subprocess.Popen(
        [str(EXE), "--port", str(PORT), "--no-browser"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        # onefile 首次要自我解压，慢一点
        deadline = time.time() + 60
        info = None
        while time.time() < deadline:
            info = probe()
            if info:
                break
            time.sleep(0.6)

        if not info:
            check("exe 能启动并服务页面", False, "60 秒内没就绪")
            return 1
        check("exe 能启动并服务页面", True, f"内部 pid={info.get('pid')}")

        # onefile = 引导父进程 + 干活的子进程
        n = proc_count()
        check("onefile 下确实是两个进程", n == 2, f"实际 {n} 个")

        # 只报关闭，不心跳 —— 宽限期一过，干活的进程先退
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/closing", data=b"", method="POST"
        )
        t_close = time.time()
        urllib.request.urlopen(req, timeout=5).read()

        # (1) 服务本身要在宽限期后很快消失
        deadline = t_close + 15
        gone_at = None
        while time.time() < deadline:
            if probe() is None:
                gone_at = time.time() - t_close
                break
            time.sleep(0.3)
        check("关掉页面后服务本身很快停掉", gone_at is not None,
              f"{gone_at:.1f} 秒" if gone_at else "15 秒内仍在响应")

        # (2) 整棵进程树要清空。注意 onefile 的引导父进程还要删掉解压出来的
        #     _MEI 目录（本机实测 831 个文件，安全组件逐个检查，要十几二十秒），
        #     所以这里给 90 秒上限，并把实测耗时打出来。
        deadline = t_close + 90
        tree_gone_at = None
        while time.time() < deadline:
            if proc_count() == 0:
                tree_gone_at = time.time() - t_close
                break
            time.sleep(0.5)
        check("整棵进程树最终清空（含 onefile 引导进程）",
              tree_gone_at is not None,
              f"耗时 {tree_gone_at:.1f} 秒" if tree_gone_at else "90 秒内仍有残留")
        check("退出码为 0", proc.poll() == 0, f"实际 {proc.poll()}")

        # (3) 不该留下本次解压的临时目录
        leftover = mei_dirs() - mei_before
        check("没有残留本次解压的临时目录", not leftover,
              f"残留 {sorted(leftover)}" if leftover else "干净")

    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, text=True, errors="ignore")
            proc.wait(timeout=10)

    print()
    print("=" * 66)
    print(f"  结果：{_PASS} 通过，{_FAIL} 失败")
    print("=" * 66)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
