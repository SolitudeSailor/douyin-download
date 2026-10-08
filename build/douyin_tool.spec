# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 抖音视频下载器。

打包命令（在项目根目录执行）：
    pyinstaller build/douyin_tool.spec --clean --noconfirm

产物：
    dist/douyin-tool.exe（单文件，无控制台窗口，带图标）

为什么需要自定义 spec 而不是直接命令行打包：
1. f2 库带数据文件（conf/ 目录、签名用 .js 文件），必须显式收集，
   否则运行时报「找不到配置文件」或签名失败。
2. f2 依赖的一堆包（gmssl / PyExecJS / jsonpath-ng / browser_cookie3）
   含动态导入，PyInstaller 静态分析扫不到，必须放进 hiddenimports。
3. Flask 的 templates 必须作为 data 打进去，否则网页 500。
4. f2 官方项目曾因打包问题放弃 exe 方案，所以这里做了完整兜底。

注意：console=False 表示隐藏控制台窗口。此时 sys.stdout/stderr 为 None，
所有 print 必须走 src/douyin_tool/console.py 的 safe_print，否则会崩。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files

# ---------------------------------------------------------------------------
# 项目根目录（本 spec 在 build/ 下）
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
WEB_DIR = os.path.join(SRC_DIR, "douyin_tool", "web")

# ---------------------------------------------------------------------------
# 收集数据文件与隐藏导入
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 0) ★ 阻止 f2 在打包过程中往项目根建 logs/ —— 必须在 collect_all 之前
# ---------------------------------------------------------------------------
# `collect_all("f2")` 会 import f2 来探测结构，而 `f2/log/logger.py` 在**模块级**
# 执行 `log_setup()`：
#     logger = logging.getLogger("f2")
#     if logger.hasHandlers(): return logger          # ← 已配置就直接跳过
#     temp_log_dir = Path("./logs"); temp_log_dir.mkdir(exist_ok=True)
# 于是**每次打包**都会在「执行 pyinstaller 时的工作目录」（通常就是项目根）
# 留下一个空的 logs/ 目录。
#
# 这就是长期挂在待办里、始终没定位到的「根 logs/ 从哪来」（R4）的真正根因 ——
# 不是运行时漏调 `_prepare_f2_env()`，而是**打包时 collect_all 的副作用**。
#
# 需要两道防线，缺一不可（实测：只做 ① 仍然会漏）：
#   ① 主进程 —— 先抢占 logger，`log_setup()` 会直接 return；
#   ② 子进程 —— PyInstaller 的 `collect_submodules` 用 `@isolated.decorate`
#      在**独立子进程**里 import 包，子进程不继承 ① 的 logging 配置，
#      但**继承 CWD**。所以只能在 collect_all 期间把 CWD 挪到临时目录。
import logging
import shutil
import tempfile

_f2_logger = logging.getLogger("f2")
if not _f2_logger.hasHandlers():
    _f2_logger.addHandler(logging.NullHandler())
    _f2_logger.propagate = False

datas = []
binaries = []
hiddenimports = []

# ★ 把 CWD 临时挪走（整个过程包在 try/finally 里）。只用它包住 collect_*，
#   不包 Analysis/EXE —— 那两步要用 --workpath / --distpath 的相对路径。
_ORIG_CWD = os.getcwd()
_TMP_CWD = tempfile.mkdtemp(prefix="dyd_pack_")
os.chdir(_TMP_CWD)
try:
    # 1) f2 全量收集：含 conf/、签名 js、proto 等数据文件
    print("[spec] 收集 f2 ...")
    for pkg in ("f2",):
        try:
            d, b, h = collect_all(pkg)
            datas += d
            binaries += b
            hiddenimports += h
            print(f"  {pkg}: {len(d)} datas, {len(b)} binaries, {len(h)} hiddenimports")
        except Exception as e:
            print(f"  [警告] collect_all({pkg}) 失败: {e}")

    # 2) 无边框窗口宿主：pywebview（Edge WebView2 渲染）
    #    - webview 自带 js/ 目录（拖动区、JS↔Python 桥），必须作为 data 打进去，
    #      否则窗口里页面正常但拖动、窗口按钮全部失效
    #    - Windows 后端走 WinForms，实际依赖 pythonnet + clr_loader（含原生 dll），
    #      属于典型的「静态分析扫不到」，必须 collect_all
    for pkg in ("webview", "pythonnet", "clr_loader", "clr", "bottle",
                "proxy_tools"):
        try:
            d, b, h = collect_all(pkg)
            datas += d
            binaries += b
            hiddenimports += h
            print(f"  {pkg}: {len(d)} datas, {len(b)} binaries, {len(h)} hiddens")
        except Exception as e:
            print(f"  [跳过] {pkg}: {e}")

    # 3) 其他含二进制/动态导入的依赖
    for pkg in ("gmssl", "PyExecJS", "jsonpath_ng", "browser_cookie3",
                "Cryptodome", "websockets", "httpx", "pydantic"):
        try:
            d, b, h = collect_all(pkg)
            datas += d
            binaries += b
            hiddenimports += h
            print(f"  {pkg}: {len(d)} datas, {len(b)} binaries")
        except Exception as e:
            print(f"  [跳过] {pkg}: {e}")
finally:
    os.chdir(_ORIG_CWD)
    # 切回来后顺手把这个临时目录删掉：不删的话每打一次包就会在 %TEMP% 里
    # 留下一个空目录（ignore_errors —— 万一还有子进程占着 CWD，留着也无害）
    shutil.rmtree(_TMP_CWD, ignore_errors=True)

# 3) 前端资源（Flask 模板 + 静态文件）
datas += [
    (os.path.join(WEB_DIR, "templates"), "douyin_tool/web/templates"),
    (os.path.join(WEB_DIR, "static"), "douyin_tool/web/static"),
]
# 应用 Python 模块由 Analysis/PYZ 收集；不能把 src 当普通资源递归打包，
# 否则 __pycache__ 的 co_filename 会把本机绝对路径带进发布程序。

# 4) 显式隐藏导入：这些是 f2 运行时按需导入的模块，静态分析容易漏
hiddenimports += [
    "f2",
    "f2.apps",
    "f2.apps.douyin",
    "f2.apps.douyin.handler",
    "f2.apps.douyin.crawler",
    "f2.apps.douyin.dl",
    "f2.apps.douyin.filter",
    "f2.apps.douyin.utils",
    "f2.apps.douyin.model",
    "f2.apps.douyin.api",
    "f2.apps.douyin.db",
    "f2.log",
    "f2.log.logger",
    "f2.utils",
    "f2.utils.utils",
    "f2.utils._singleton",
    "yaml",
    "flask",
    "jinja2",
    "werkzeug",
    "sqlite3",
    "asyncio",
    # CDP 自动登录用（websockets 的 legacy 客户端是动态加载的，静态分析易漏）
    "websockets",
    "websockets.legacy",
    "websockets.legacy.client",
    "websockets.legacy.handshake",
    "websockets.legacy.protocol",
    # 无边框窗口：Windows 上用的是 WinForms + Edge WebView2 后端
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
block_cipher = None

a = Analysis(
    [os.path.join(SRC_DIR, "douyin_tool", "cli.py")],
    pathex=[PROJECT_ROOT, SRC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除明显用不到的大件，减小体积
        "matplotlib", "tkinter", "PIL", "cv2", "numpy",
        "pandas", "scipy", "IPython", "notebook",
        "pytest", "black", "setuptools", "pip",
        # pywebview 的非 Windows 后端：collect_all 会把它们一并列进 hiddenimports，
        # 但它们依赖 gi / PyQt5 / Cocoa，本机（WinForms + WebView2）用不到
        "webview.platforms.gtk",
        "webview.platforms.qt",
        "webview.platforms.cocoa",
        "webview.platforms.android",
        "webview.platforms.tk",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="douyin-tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 会让杀软更容易误报，关掉
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 隐藏控制台：双击只出浏览器界面
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, "icon.ico"),   # 图标（build/icon.ico）
)
