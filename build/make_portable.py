# -*- coding: utf-8 -*-
"""把主程序和面向接收者的说明打成绿色免安装 zip（对外分享用）。

设计要点：**只打包显式列出的文件，绝不扫描目录**。

分享包最危险的事故是把自己的登录态一起发出去。主程序所在的 dist\\ 里
同时存着 config.yaml（Cookie）、browser_profile（登录态）、Download（视频）
等运行时数据 —— 只要用「扫描目录」的写法，迟早会把它们打进去。
这里反过来：进包清单写死在 PAYLOAD 里，多一个文件都进不来。

注意：不要再引入 release/app/ 之类的「发布源副本目录」。
dist\\douyin-tool.exe 就是唯一的主程序，复制一份只会带来
「忘记同步 → 发出去的是旧版」的隐患。
"""
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "release" / "抖音视频下载器-绿色版.zip"
TOP = "抖音视频下载器"

# ★ 进包清单：加文件必须同步加到这里（宁可报错，不可漏发）
PAYLOAD = [
    (BASE / "dist" / "douyin-tool.exe", "douyin-tool.exe"),   # PyInstaller 产物
    (BASE / "build" / "使用说明.txt", "使用说明.txt"),          # 面向接收者的说明
]


def main() -> int:
    missing = [str(src) for src, _ in PAYLOAD if not src.is_file()]
    if missing:
        print("[错误] 缺少打包素材：")
        for m in missing:
            print("   -", m)
        print("\n提示：主程序要先跑 build\\build.bat 生成 dist\\douyin-tool.exe。")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    print("[1/2] 打包 ...")
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, arcname in PAYLOAD:
            z.write(src, f"{TOP}/{arcname}")
            print(f"       + {TOP}/{arcname}  ({src.stat().st_size:,} 字节)")

    print("[2/2] 完成")
    print(f"       产物: {OUT}")
    print(f"       大小: {OUT.stat().st_size / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
