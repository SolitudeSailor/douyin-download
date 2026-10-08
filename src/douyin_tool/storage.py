"""本地文件事务：线程/进程互斥，以及同目录原子替换。"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class PersistenceError(OSError):
    """写入失败时交给调用者处理，避免界面误报保存成功。"""


_LOCK = threading.RLock()
_LOCAL = threading.local()


@contextmanager
def locked_file(path: Path, timeout: float = 15.0):
    """同一数据文件的读改写事务可重入，进程退出时锁由系统释放。

    锁放在独立的 .lock 文件中，不能锁住会被 os.replace 替换的数据文件。
    """
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _LOCK:
        held = getattr(_LOCAL, "held", None)
        if held is None:
            held = _LOCAL.held = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".lock", "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise PersistenceError(f"等待数据文件锁超时：{path.name}") from exc
                    time.sleep(0.05)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, text: str) -> None:
    """先完整写入并刷新临时文件，再替换目标；失败时旧文件仍然可读。"""
    path = Path(path)
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        raise PersistenceError(f"无法保存 {path.name}，请检查目录权限和磁盘空间。") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
