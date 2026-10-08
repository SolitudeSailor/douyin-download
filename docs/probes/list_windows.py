# -*- coding: utf-8 -*-
"""列出某个进程的所有顶层窗口（含不可见的），排查「窗口没出现」。

用法：
    python docs\\probes/list_windows.py [pid]
    python docs\\probes/list_windows.py            # 自动取 running.json 里的 pid
"""
from __future__ import annotations

import ctypes
import json
import pathlib
import sys
from ctypes import wintypes

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if pid is None:
        from douyin_tool.config import get_data_dir
        try:
            pid = int(json.loads((get_data_dir() / "running.json")
                                 .read_text(encoding="utf-8"))["pid"])
        except Exception as e:  # noqa: BLE001
            print(f"读不到 running.json：{e}")
            return 1

    u = ctypes.windll.user32
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_ssize_t)

    found = []

    def cb(h, _):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value != pid:
            return True
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, cls, 256)
        r = wintypes.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        found.append({"hwnd": int(h), "title": buf.value, "class": cls.value,
                      "visible": bool(u.IsWindowVisible(h)),
                      "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top)})
        return True

    u.EnumWindows(proc(cb), 0)
    print(f"pid={pid} 的顶层窗口共 {len(found)} 个：")
    for f in found:
        print(f"  hwnd={f['hwnd']:#x} visible={f['visible']!s:5} "
              f"rect={f['rect']} class={f['class'][:40]!r}")
        print(f"      title={f['title']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
