@echo off
chcp 65001 >nul
rem 与调用脚本共享 PY；支持显式覆盖、项目环境、已激活环境和本机旧环境。
if defined DOUYIN_PYTHON set "PY=%DOUYIN_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY if defined VIRTUAL_ENV set "PY=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
    echo [错误] 未找到 Python。请建立 .venv 或设置 DOUYIN_PYTHON。
    exit /b 1
)
"%PY%" -c "import importlib.util; missing=[m for m in ('flask','yaml','f2','webview','websockets','PyInstaller') if importlib.util.find_spec(m) is None]; print('缺少依赖: '+', '.join(missing)) if missing else None; raise SystemExit(bool(missing))"
if errorlevel 1 (
    echo [错误] 请用所选 Python 安装 requirements-dev.txt 中的依赖。
    exit /b 1
)
exit /b 0
