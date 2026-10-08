# -*- coding: utf-8 -*-
"""深度泄露审计 —— 解开压缩层后再扫，补上 audit_release.py 的假阴性盲点。

【为什么需要这个脚本】
`audit_release.py` 是在产物的**原始字节流**上找明文敏感串。
但 PyInstaller onefile exe 的 CArchive 条目几乎全是 zlib 压缩的，
Python 模块字节码又压在 PYZ 里 —— 压缩流里根本找不到明文字符串，
于是「找不到 = 干净」这条判断会**假通过**（false negative）。

本脚本把每个条目解压出来再扫：
  · PyInstaller exe → 解开 CArchive 全部条目 + PYZ 内每个模块的字节码
  · 其他产物（如 Inno Setup 的 setup.exe）→ 只能查明文层，会明确告知

【关键判据：区分「本机泄露」与「上游构建路径」】
解压后**必然**会看到大量 `C:\\Users\\xxx\\...`，那是 CPython / OpenSSL /
cryptography 官方发布包里残留的 PDB 构建路径（runneradmin = GitHub Actions，
Administrator = CPython 构建机）。这属于通用现象，与本机无关。
真正的泄露是**本机用户名**出现。所以本脚本按用户名精确比对，
把上游构建路径单独归类为「信息」而非「失败」。

用法：
    python build/audit_deep_scan.py                       # 默认扫 dist/douyin-tool.exe
    python build/audit_deep_scan.py <exe 或目录>
退出码非 0 表示发现真实泄露，可直接挂进打包脚本。
"""
from __future__ import annotations

import getpass
import io
import marshal
import os
import re
import struct
import sys
import zlib
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = BASE / "dist" / "douyin-tool.exe"

ENCODINGS = ("utf-8", "gbk", "utf-16-le")


# ---------------------------------------------------------------------------
# 敏感串：只放「一定是本机私有」的东西，不放大而化之的前缀
# ---------------------------------------------------------------------------
def build_needles() -> list[tuple[str, bytes]]:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser()
    home = str(Path.home())
    project = str(BASE)
    sep = os.sep

    raw: list[tuple[str, str]] = [
        ("本机用户名", user),
        ("用户主目录", home),
        ("项目绝对路径", project),
        # 目录名用「带分隔符」的形式，避免裸词误命中
        # （如项目名「爬虫」是通用中文词，f2 源码注释里就出现过）
        ("项目目录路径片段", f"{sep}{BASE.name}{sep}"),
        ("项目目录路径片段(末尾)", BASE.name + sep),
        ("父目录路径片段", f"{sep}{BASE.parent.name}{sep}"),
        ("本机用户目录", f"C:{sep}Users{sep}{user}"),
    ]

    needles: list[tuple[str, bytes]] = []
    for label, s in raw:
        if not s or len(s) < 5:
            continue
        for enc in ENCODINGS:
            try:
                b = s.encode(enc)
            except (UnicodeEncodeError, LookupError):
                continue
            needles.append((f"{label} [{enc}]", b))

    # data/config.yaml 里的真实 Cookie 值（登录凭证本体）
    cfg = BASE / "data" / "config.yaml"
    if cfg.is_file():
        try:
            text = cfg.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"([A-Za-z0-9_]{2,})=([A-Za-z0-9_%\-\.]{12,})", text):
                needles.append((f"Cookie 值 [{m.group(1)}=…]", m.group(2).encode("utf-8")))
        except OSError:
            pass

    return needles


def scan_blob(label: str, data: bytes, needles) -> list[str]:
    return [f"{label}  ← {lbl}" for lbl, nd in needles if nd in data]


# ---------------------------------------------------------------------------
# PyInstaller 归档解析
# ---------------------------------------------------------------------------
def walk_pyz(pyz_bytes: bytes, needles) -> tuple[list[str], int]:
    """解析 PYZ（PyInstaller 的 ZlibArchive）并逐个模块解压扫描。"""
    if pyz_bytes[:4] != b"PYZ\0":
        return [f"PYZ magic 异常: {pyz_bytes[:8]!r}"], 0
    toc_pos = struct.unpack("!i", pyz_bytes[8:12])[0]
    try:
        toc_items = marshal.load(io.BytesIO(pyz_bytes[toc_pos:]))
    except Exception as exc:  # noqa: BLE001
        return [f"PYZ TOC 解析失败: {exc}"], 0

    hits: list[str] = []
    n = 0
    for name, entry in toc_items:
        if not isinstance(entry, tuple) or len(entry) != 3:
            continue
        typ, pos, length = entry
        n += 1
        if typ != "z":            # 仅 'z' 类型（zlib 压缩的模块字节码）需要解压
            continue
        try:
            raw = zlib.decompress(pyz_bytes[pos:pos + length])
        except zlib.error:
            continue
        hits += scan_blob(f"PYZ:{name}", raw, needles)
    return hits, n


def iter_pyinstaller_entries(exe: Path):
    """产出 (显示名, 解压后字节)。需要 PyInstaller（打包环境里必然有）。"""
    from PyInstaller.archive.readers import CArchiveReader

    car = CArchiveReader(str(exe))
    for name in list(car.toc):
        data = car.extract(name)
        if name.lower().endswith(".pyz"):
            yield f"[PYZ] {name}", data, True
        else:
            yield name, data, False


# ---------------------------------------------------------------------------
# 上游构建路径画像（信息，不算失败）
# ---------------------------------------------------------------------------
USERPATH_RE = re.compile(rb"C:\\Users\\([ -~]{0,60})")


def profile_upstream_usernames(blobs, me: str) -> tuple[dict[str, int], list[str]]:
    counter: Counter[str] = Counter()
    mine: list[str] = []
    for label, data in blobs:
        for m in USERPATH_RE.finditer(data):
            uname = m.group(1).split(b"\\")[0].split(b"/")[0]
            uname = uname.decode("latin-1", "ignore")
            uname = "".join(ch for ch in uname if ch.isprintable())[:40]
            if not uname:
                continue
            counter[uname] += 1
            if uname.lower() == me.lower():
                mine.append(label)
    return dict(counter), mine


# ---------------------------------------------------------------------------
def audit(exe: Path) -> int:
    me = os.environ.get("USERNAME") or getpass.getuser()
    needles = build_needles()

    print("=" * 64)
    print("  深度泄露审计（解压后扫描）")
    print("=" * 64)
    print(f"  目标    : {exe}")
    print(f"  本机用户: {me}")
    print(f"  敏感串  : {len(needles)} 条（UTF-8 / GBK / UTF-16LE 三编码）")

    if not exe.is_file():
        print(f"\n   ✗ 文件不存在: {exe}")
        return 2

    # 判定是不是 PyInstaller 产物：看有没有 PKG/CArchive 结构
    head = exe.read_bytes()[:2]
    is_pe = head == b"MZ"
    blobs: list[tuple[str, bytes]] = []
    hits: list[str] = []
    n_pyz_modules = 0
    mode = "明文层（非 PyInstaller 产物，压缩内容无法展开）"

    if is_pe:
        try:
            entries = list(iter_pyinstaller_entries(exe))
            mode = f"PyInstaller 归档（{len(entries)} 个条目）"
            for label, data, is_pyz in entries:
                if is_pyz:
                    h, n = walk_pyz(data, needles)
                    hits += h
                    n_pyz_modules += n
                else:
                    hits += scan_blob(label, data, needles)
                blobs.append((label, data))
        except Exception as exc:  # noqa: BLE001
            print(f"\n   ! 不是 PyInstaller 归档（{type(exc).__name__}: {exc}）")
            print("     退回明文层扫描 —— 压缩内容这次没有被覆盖")
            blobs = [(exe.name, exe.read_bytes())]
            hits = scan_blob(exe.name, exe.read_bytes(), needles)
    else:
        blobs = [(exe.name, exe.read_bytes())]
        hits = scan_blob(exe.name, exe.read_bytes(), needles)

    print(f"  模式    : {mode}")
    if n_pyz_modules:
        print(f"  PYZ 模块: {n_pyz_modules} 个（逐模块解压后扫描）")

    # ---- 上游构建路径画像 ----
    users, mine = profile_upstream_usernames(blobs, me)
    print("\n[1/2] 上游构建路径画像（信息项，非失败）")
    if users:
        for u, c in sorted(users.items(), key=lambda x: -x[1]):
            tip = "  ← 本机用户名！" if u.lower() == me.lower() else ""
            print(f"   {c:5d} ×  C:\\Users\\{u}{tip}")
        if not mine:
            print("   ✓ 无一是本机用户名 —— 属 CPython/OpenSSL/cryptography")
            print("     官方发布包残留的 PDB 路径，与接收方无关")
    else:
        print("   （无 C:\\Users\\ 路径）")

    # ---- 敏感串结论 ----
    print("\n[2/2] 敏感串结论")
    if hits:
        print(f"   ✗ 发现 {len(hits)} 处真实泄露：")
        for h in hits[:60]:
            print("      -", h)
    else:
        print("   ✓ 全部条目（含每个 PYZ 模块）均未命中本机用户名 / 项目路径 / Cookie")

    failed = bool(hits) or bool(mine)
    print("\n" + "=" * 64)
    print("  ✗ 未通过：产物含本机私有信息，不可对外分享" if failed
          else "  ✓ 通过：未发现本机私有信息")
    print("=" * 64)
    return 1 if failed else 0


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TARGET
    if target.is_dir():
        exes = sorted(target.glob("*.exe"))
        if not exes:
            print(f"目录里没有 .exe: {target}")
            return 2
        rc = 0
        for e in exes:
            rc |= audit(e)
            print()
        return rc
    return audit(target)


if __name__ == "__main__":
    sys.exit(main())
