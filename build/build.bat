@echo off
chcp 65001 >nul
setlocal

echo ========================================
echo   抖音视频下载器 - 一键打包
echo ========================================
echo.

cd /d "%~dp0.."
call build\resolve_python.bat
if errorlevel 1 exit /b 1

echo [1/3] 准备候选构建，保留现有主程序...
set "PYINSTALLER_CONFIG_DIR=%CD%\build_tmp\pyinstaller_cache"

echo [2/3] 开始打包（首次约需 1-3 分钟）...
rem --workpath build_tmp：把 PyInstaller 的中间产物（.toc / .pkg / PYZ / base_library.zip）
rem 从 build\ 挪到 build_tmp\，让 build\ 只保留「人写的」三样东西：
rem douyin_tool.spec / build.bat / icon.ico
"%PY%" -X utf8 -m PyInstaller build\douyin_tool.spec --noconfirm --workpath build_tmp\pyinstaller --distpath dist_test
if errorlevel 1 (
    echo.
    echo [失败] 打包出错，请查看上方日志。
    pause
    exit /b 1
)

echo.
echo [3/3] 审计并更新主程序...
"%PY%" -X utf8 build\audit_deep_scan.py dist_test\douyin-tool.exe
if errorlevel 1 exit /b 1
if not exist dist mkdir dist
copy /y "dist_test\douyin-tool.exe" "dist\douyin-tool.exe" >nul
if errorlevel 1 exit /b 1
echo.
echo   产物位置：dist\douyin-tool.exe
echo   图标文件：build\icon.ico
echo   中间产物：build_tmp\（可随时删除，下次打包自动重建）
echo.
echo   提示：双击 exe 即可启动，首次使用点网页上的「打开登录窗口」扫码登录。
echo   详细说明见 docs\使用说明.md
echo.
pause
