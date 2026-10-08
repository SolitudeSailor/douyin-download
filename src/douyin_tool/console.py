# -*- coding: utf-8 -*-
"""控制台输出兼容层 + 全局异常兜底。

为什么需要这个模块：

PyInstaller 用 `--noconsole`（windowed 模式）打包后，Windows 不分配控制台，
此时 `sys.stdout` / `sys.stderr` 会变成 **None**。任何 `print(...)` 都会抛：

    AttributeError: 'NoneType' object has no attribute 'write'

结果就是程序一启动就静默死掉，用户看不到任何提示。

本模块提供：
1. `safe_print()` —— 无论有没有控制台都不会崩的输出函数。
2. `init_console()` —— 尽早调用，把 stdout/stderr 兜底成哑对象。
3. `install_excepthook()` —— 崩溃时弹 Windows 消息框 + 写日志文件，
   避免「双击没反应」这种最难排查的体验。

同时兼容开发态（有控制台）与冻结态（无控制台）两种环境。
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 哑流对象：替换掉 None 的 stdout/stderr
# ---------------------------------------------------------------------------
class _NullStream:
    """黑洞流：吞掉所有写入，不报错。"""

    encoding = "utf-8"
    errors = "replace"

    def write(self, s: Any) -> int:  # noqa: D102
        return len(s) if isinstance(s, str) else 0

    def writelines(self, lines) -> None:  # noqa: D102
        pass

    def flush(self) -> None:  # noqa: D102
        pass

    def isatty(self) -> bool:  # noqa: D102
        return False

    def fileno(self):  # noqa: D102
        raise OSError("no fileno")

    def close(self) -> None:  # noqa: D102
        pass


def init_console() -> None:
    """把 None 的 stdout/stderr 兜底成哑流。尽早调用。"""
    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]


def has_console() -> bool:
    """判断当前是否真有可见控制台。"""
    return not isinstance(sys.stdout, _NullStream) and sys.stdout is not None


def safe_print(*args: Any, **kwargs: Any) -> None:
    """`print` 的安全版本：无控制台时静默丢弃，绝不抛异常。"""
    if not has_console():
        return
    try:
        print(*args, **kwargs)  # noqa: T201
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 日志文件（崩溃时写这里，便于事后排查）
# ---------------------------------------------------------------------------
def _log_path(name: str = "error.log") -> Path:
    from douyin_tool import config as cfg_mod
    try:
        # 走数据目录：开发态 = <项目根>/data/logs，冻结态 = exe 同级/logs
        base = cfg_mod.get_data_dir()
    except Exception:  # noqa: BLE001
        base = Path(os.getcwd())
    logs = base / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        return base / name
    return logs / name


def write_error_log(text: str) -> Path:
    """把错误信息追加写入 logs/error.log，返回日志路径。"""
    p = _log_path()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 60}\n[{stamp}]\n{text}\n")
    except OSError:
        pass
    return p


def log_event(text: str) -> Path:
    """把一条运行事件追加写入 logs/app.log。

    为什么需要它：打包成 `--noconsole` 之后 `sys.stdout` 是 None，
    `safe_print()` 直接变成空操作 —— 像「程序为什么自己退出了」这种问题
    在用户机器上不会有任何线索。关键节点必须落盘。
    """
    p = _log_path("app.log")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except OSError:
        pass
    return p


# ---------------------------------------------------------------------------
# 崩溃兜底：弹消息框（无控制台时用户唯一能看到的提示）
# ---------------------------------------------------------------------------
def _show_message_box(title: str, text: str) -> bool:
    """用 Windows 原生 API 弹消息框。非 Windows 或失败返回 False。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        # MB_ICONERROR(0x10) | MB_SETFOREGROUND(0x10000) | MB_TOPMOST(0x40000)
        ctypes.windll.user32.MessageBoxW(
            None, text, title, 0x10 | 0x10000 | 0x40000
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def install_excepthook() -> None:
    """安装全局异常钩子：写日志 + 弹框，避免静默崩溃。"""

    def _hook(exc_type, exc_value, exc_tb):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_file = write_error_log(detail)

        # 控制台还在就直接打印
        if has_console():
            try:
                print(detail, file=sys.stderr)
                print(f"\n[错误日志] {log_file}", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass

        msg = (
            f"程序遇到未处理的错误，已中止。\n\n"
            f"{exc_type.__name__}: {exc_value}\n\n"
            f"详细日志：\n{log_file}"
        )
        _show_message_box("抖音视频下载器 - 出错了", msg)

    sys.excepthook = _hook

    # 线程里的异常不会走 sys.excepthook，单独兜一层
    try:
        import threading

        def _thread_hook(args):  # noqa: ANN001
            if issubclass(args.exc_type, SystemExit):
                return
            detail = "".join(
                traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback
                )
            )
            log_file = write_error_log(f"[线程 {args.thread.name}]\n{detail}")
            if has_console():
                try:
                    print(detail, file=sys.stderr)
                except Exception:  # noqa: BLE001
                    pass
            _show_message_box(
                "抖音视频下载器 - 后台任务出错",
                f"{args.exc_type.__name__}: {args.exc_value}\n\n详细日志：\n{log_file}",
            )

        threading.excepthook = _thread_hook
    except Exception:  # noqa: BLE001
        pass
