"""独立测试的隔离环境；所有 f2 回归测试禁止访问外网。"""

import atexit
import os
import socket
import tempfile
from pathlib import Path


def isolate_data():
    if "DOUYIN_DATA_DIR" not in os.environ:
        original_cwd = Path.cwd()
        temp = tempfile.TemporaryDirectory(prefix="dyd_tests_")
        os.environ["DOUYIN_DATA_DIR"] = temp.name

        def cleanup():
            os.chdir(original_cwd)
            temp.cleanup()
        atexit.register(cleanup)


def prepare_offline_f2():
    isolate_data()
    from douyin_tool import core
    core._prepare_f2_env()
    from f2.apps.douyin.utils import TokenManager
    # f2 model 在模块级生成 msToken；仅测试中替换，生产登录/签名不受影响。
    TokenManager.gen_real_msToken = staticmethod(lambda *a, **kw: "offline_test_token" * 8)

    # Windows asyncio.socketpair 需要连接本机回环地址，允许它但禁止外网/DNS。
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(method):
        def connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
                raise AssertionError("离线测试不允许访问外网。")
            return method(sock, address)
        return connect
    socket.socket.connect = guard(real_connect)
    socket.socket.connect_ex = guard(real_connect_ex)
