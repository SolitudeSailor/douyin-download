@echo off
chcp 65001 >nul
cd /d "%~dp0.."

set "PY=%DOUYIN_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY if defined VIRTUAL_ENV set "PY=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"

if not defined PY (
    echo [错误] 未找到 Python。请建立 .venv 或设置 DOUYIN_PYTHON。
    pause
    exit /b 1
)

"%PY%" "secure\unseal.py" %*
echo.
pause
