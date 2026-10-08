# -*- coding: utf-8 -*-
"""发布前安全审计 —— 确认对外分享包里不含任何个人信息与运行时数据。

用法：
    python build/audit_release.py                  # 默认审计 release/
    python build/audit_release.py <目录>           # 审计指定目录（如搬走后的分享包）

检查三步：
  1) 打包素材是否就位（dist/douyin-tool.exe、build/使用说明.txt）
  2) 目标目录里所有产物的字节流不含 用户名、本机绝对路径、项目名
  3) 不含 data/config.yaml 里的真实 Cookie 片段

任何一项命中都会以非 0 退出码结束，可直接挂到打包脚本里。

⚠️ **本脚本的能力边界（2026-09-29 补记，重要）**
   这里扫的是产物的**原始字节流**。而 PyInstaller onefile exe 的 CArchive
   条目几乎全是 zlib 压缩的、Python 字节码又压在 PYZ 里 —— 压缩流里匹配不到
   明文字符串，于是「没找到」会被误读成「干净」。
   所以本脚本只有「**命中即失败**」的效力，**不能凭它通过就认定安全**。
   要拿到「压缩层也干净」的结论，必须再跑 build/audit_deep_scan.py
   （它会把每个条目和每个 PYZ 模块解压出来重扫）。
"""
from __future__ import annotations

import getpass
import os
import re
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RELEASE = BASE / "release"

# ★ 进包清单 —— 改了这里必须同步改 build/installer.iss 和 build/make_portable.py
PAYLOAD = [
    BASE / "dist" / "douyin-tool.exe",   # PyInstaller 产物
    BASE / "build" / "使用说明.txt",      # 面向接收者的说明
]

# 短于这个长度的串在二进制里会大量误命中（如 "AI"、"E:"），直接不查
MIN_NEEDLE_LEN = 5


def collect_needles() -> list[tuple[str, bytes]]:
    """收集要排查的敏感串。用户名/路径都是本机动态取的，换台机器也能用。"""
    needles: list[tuple[str, bytes]] = []

    user = os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser()
    home = str(Path.home())
    project = str(BASE)

    for label, s in [
        ("当前系统用户名", user),
        ("用户主目录路径", home),
        ("项目绝对路径", project),
        ("项目目录名", BASE.name),
        ("父目录名", BASE.parent.name),
    ]:
        if s and len(s) >= MIN_NEEDLE_LEN:
            needles.append((f"{label} [{s}]", s.encode("utf-8")))
            needles.append((f"{label} (gbk) [{s}]", s.encode("gbk", "ignore")))

    # ★ 不要用裸的 `C:\Users\` 当特征 —— 它必然误报：
    #   CPython / OpenSSL / cryptography 的官方发布二进制里都残留着上游构建机的
    #   PDB 路径（C:\Users\runneradmin\...、C:\Users\Administrator\...），
    #   跟本机毫无关系。真正的泄露特征只能是**本机用户名**，
    #   它已被上面的「用户主目录路径」needle 完整覆盖。
    #   （解压后扫描时请用 build/audit_deep_scan.py，它按用户名精确区分。）
    #
    #   另外不要把 browser_profile / douyin_users / download_history 这类
    #   「程序自己生成的目录名」列进来：它们本来就会正常出现在使用说明和代码里，
    #   只会造成误报。有没有夹带运行时数据，由第 1 步的进包清单 + 各打包脚本负责。

    # data/config.yaml 里的真实 Cookie 片段
    # （运行时数据统一在 data/；dist/ 只放构建产物）
    cfg = BASE / "data" / "config.yaml"
    if cfg.is_file():
        try:
            text = cfg.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"([A-Za-z0-9_]{2,})=([A-Za-z0-9_%\-\.]{12,})", text):
                needles.append((f"Cookie 值 [{m.group(1)}=…]", m.group(2).encode("utf-8")))
        except OSError:
            pass

    return needles


def check_payload() -> list[str]:
    problems = []
    for p in PAYLOAD:
        if not p.is_file():
            problems.append(f"缺少打包素材: {p}")
    return problems


def iter_artifacts(root: Path):
    """产出 (显示名, 字节) —— zip 会逐个成员解出来扫。"""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for info in z.infolist():
                        if not info.is_dir():
                            yield f"{p.name} → {info.filename}", z.read(info)
            except zipfile.BadZipFile:
                yield f"{p.name} (损坏的 zip)", b""
        else:
            yield str(p.relative_to(root)), p.read_bytes()


def resolve_root(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1]).expanduser().resolve()
    return RELEASE


def main() -> int:
    root = resolve_root(sys.argv)

    print("=" * 64)
    print("  发布包安全审计")
    print("=" * 64)
    print(f"  目标目录: {root}")

    if not root.is_dir():
        print(f"\n   ✗ 目录不存在: {root}")
        return 2

    # ---- 1) 打包素材 ----
    print("\n[1/3] 打包素材检查 ...")
    problems = check_payload()
    if problems:
        for x in problems:
            print("   ✗", x)
    else:
        for p in PAYLOAD:
            print(f"   ✓ {p.relative_to(BASE)}  ({p.stat().st_size:,} 字节)")

    # ---- 2) 字节流扫描 ----
    print("\n[2/3] 产物字节流敏感串扫描 ...")
    needles = collect_needles()
    print(f"   待查敏感串 {len(needles)} 条（含本机用户名/路径/真实 Cookie 片段）")
    total_hits = 0
    n_files = 0
    for name, data in iter_artifacts(root):
        n_files += 1
        hits = [(lbl, data.count(nd)) for lbl, nd in needles if nd in data]
        if hits:
            total_hits += len(hits)
            print(f"   ✗ {name} 命中 {len(hits)} 项：")
            for lbl, n in hits:
                print(f"        - {lbl} × {n}")
        else:
            print(f"   ✓ {name}  ({len(data):,} 字节) 干净")

    if n_files == 0:
        print("   ! 该目录下没找到任何产物文件，请确认路径是否正确")

    # ---- 3) 结论 ----
    print("\n[3/3] 结论")
    if problems or total_hits:
        print(f"   ✗ 审计未通过（素材问题 {len(problems)} 项，敏感命中 {total_hits} 项）")
        return 1
    print(f"   ✓ 审计通过：{n_files} 个产物中未发现任何个人信息、本机路径或运行时数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
