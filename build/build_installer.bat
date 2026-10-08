@echo off
chcp 65001 >nul
setlocal

echo ========================================
echo   抖音视频下载器 - 生成对外分享包
echo ========================================
echo.

cd /d "%~dp0.."

call build\resolve_python.bat
if errorlevel 1 exit /b 1
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe

if not exist "%ISCC%" (
    echo [错误] 找不到 Inno Setup 编译器：
    echo        %ISCC%
    echo.
    echo 安装命令：
    echo        winget install JRSoftware.InnoSetup --scope user
    pause
    exit /b 1
)

if not exist "dist\douyin-tool.exe" (
    echo [错误] 找不到主程序：dist\douyin-tool.exe
    echo        请先跑 build\build.bat 打包主程序。
    pause
    exit /b 1
)

echo 验证主程序压缩层 ...
"%PY%" -X utf8 "build\audit_deep_scan.py" "dist\douyin-tool.exe"
if errorlevel 1 exit /b 1

echo [1/4] 编译安装程序 ...
rem installer.iss 的 [Files] 里写死只取 dist\douyin-tool.exe + build\使用说明.txt
"%ISCC%" "build\installer.iss"
if errorlevel 1 (
    echo.
    echo [失败] 安装程序编译出错，请查看上方日志。
    pause
    exit /b 1
)

echo.
echo [2/4] 打包绿色免安装版 ...
"%PY%" "build\make_portable.py"
if errorlevel 1 (
    echo.
    echo [失败] 绿色包生成失败。
    pause
    exit /b 1
)

echo.
echo [3/4] 同步到分享目录 ...
"%PY%" -X utf8 "build\audit_release.py"
if errorlevel 1 exit /b 1
rem 分享包最终存放位置。想换地方就改这一行。
set "SHARE_DIR=%DOUYIN_SHARE_DIR%"
if not defined SHARE_DIR (
    echo    [跳过] 未设置 DOUYIN_SHARE_DIR，产物保留在 release 目录。
) else if exist "%SHARE_DIR%" (
    copy /y "release\抖音视频下载器-安装程序.exe" "%SHARE_DIR%" >nul && echo    [OK] 安装程序已同步
    copy /y "release\抖音视频下载器-绿色版.zip" "%SHARE_DIR%" >nul && echo    [OK] 绿色版已同步
) else (
    echo    [跳过] 分享目录不存在：%SHARE_DIR%
)

echo.
echo [4/4] 完成！产物：
echo.
for %%F in ("release\抖音视频下载器-安装程序.exe" "release\抖音视频下载器-绿色版.zip") do (
    if exist "%%~F" (
        for %%A in ("%%~F") do echo    %%~nxA   %%~zA 字节
    )
)
echo.
echo   分享目录：%SHARE_DIR%
echo.
echo   建议再跑一次安全审计：
echo       python build\audit_release.py
echo       python build\audit_release.py "%SHARE_DIR%"
echo.
echo   包内不含任何账号信息。
echo.
pause
