# -*- coding: utf-8 -*-
"""凭据保险箱 · 封存脚本。

把项目里所有「含账号凭据 / 浏览器登录态」的数据，打包成一个 AES-256
加密的 7z 归档（``secure/vault.7z``），密码由你自己设。

用法：
    python secure/seal.py              # 交互输入密码，只归档、不删明文
    python secure/seal.py --purge      # 封存后删除原位明文（真·锁起来）
    set DYD_VAULT_PASSWORD=xxx && python secure/seal.py     # 用环境变量取密码

设计原则（都是踩过坑才写死的）：
  * 进包清单**写死**在 CREDENTIALS 里，绝不扫描目录 —— 宁可漏收，不可误收。
    这与 build/make_portable.py 同一套思路。
  * 密码不落盘、不写进 manifest。推荐交互输入或环境变量。
    （``-p`` 只为自动化测试留的口子，会进命令行历史，别日常用。）
  * 默认**只归档、不删明文**。程序运行时必须能直接读到明文，
    把在线数据加密会让 exe 直接失效 —— 所以「加密」做成显式的封存动作，
    而不是常态。想真封存就加 ``--purge``。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SECURE = BASE / "secure"
VAULT = SECURE / "vault.7z"
MANIFEST = SECURE / "vault.manifest.json"

# ★ 凭据清单：改这里必须想清楚 —— 每多一项，误泄露面就大一分。
# 运行时数据统一收在 data\ 一处（dist\ 只放构建产物，另有 data_dir.txt 指过来），
# 所以这里只有 4 项，不再需要冻结态那一份。
CREDENTIALS: list[tuple[str, str]] = [
    ("data/config.yaml", "主配置（含登录 Cookie）"),
    ("data/browser_profile", "浏览器登录态"),
    ("data/douyin_users.db", "f2 用户缓存库"),
    ("data/download_history.json", "增量下载记录"),
]


def collect() -> list[tuple[Path, str, str]]:
    """返回 [(绝对路径, 相对路径, 说明)]，跳过不存在的。"""
    out: list[tuple[Path, str, str]] = []
    for rel, desc in CREDENTIALS:
        p = BASE / rel
        if p.exists():
            out.append((p, rel, desc))
    return out


def iter_files(items: list[tuple[Path, str, str]]):
    """把目录展开成 (文件绝对路径, 归档内相对路径)。"""
    for p, rel, _ in items:
        if p.is_file():
            yield p, rel
        else:
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    yield f, f"{rel}/{f.relative_to(p).as_posix()}"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.2f} GB"


def resolve_password(args) -> str:
    if args.password:
        return args.password
    env = os.environ.get("DYD_VAULT_PASSWORD")
    if env:
        return env
    p1 = getpass.getpass("设置保险箱密码: ")
    p2 = getpass.getpass("再输一次确认: ")
    if p1 != p2:
        print("[错误] 两次输入不一致。")
        sys.exit(1)
    if not p1:
        print("[错误] 密码不能为空。")
        sys.exit(1)
    return p1


def main() -> int:
    ap = argparse.ArgumentParser(description="凭据保险箱 · 封存")
    ap.add_argument("--purge", action="store_true",
                    help="封存后删除原位明文（真·锁起来；要用了先解封）")
    ap.add_argument("--yes", action="store_true", help="--purge 时跳过删除确认")
    ap.add_argument("-p", "--password", help="密码（不推荐：会进命令行历史）")
    args = ap.parse_args()

    try:
        import py7zr
    except ImportError:
        print("[错误] 缺少 py7zr，请先安装：")
        print('       pip install py7zr')
        return 1

    items = collect()
    print("=" * 62)
    print("  凭据保险箱 · 封存")
    print("=" * 62)
    if not items:
        print("  没找到任何凭据文件 —— 无需封存。")
        return 0

    print(f"  待封存 {len(items)} 项：")
    total = 0
    for p, rel, desc in items:
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        total += size
        kind = "目录" if p.is_dir() else "文件"
        print(f"    - {rel:<34} {desc}  [{kind} {human(size)}]")
    print(f"  合计：{human(total)}")

    pwd = resolve_password(args)
    SECURE.mkdir(parents=True, exist_ok=True)

    tmp = VAULT.with_suffix(".7z.tmp")
    if tmp.exists():
        tmp.unlink()

    print(f"\n  加密打包 -> {VAULT.name} ...")
    n_files = 0
    with py7zr.SevenZipFile(tmp, "w", password=pwd, header_encryption=True) as z:
        for src, arcname in iter_files(items):
            z.write(src, arcname)
            n_files += 1
    tmp.replace(VAULT)
    print(f"  ✓ 完成：{VAULT}")
    print(f"    {n_files} 个文件 -> {human(VAULT.stat().st_size)}（AES-256，含文件名加密）")

    MANIFEST.write_text(json.dumps({
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "vault": VAULT.name,
        "vault_size": VAULT.stat().st_size,
        "file_count": n_files,
        "purged": bool(args.purge),
        "entries": [{"path": rel, "desc": desc, "kind": "dir" if (BASE / rel).is_dir() else "file"}
                    for _, rel, desc in items],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.purge:
        print("\n  ⚠ --purge：即将删除以下原位明文：")
        for _, rel, _ in items:
            print(f"    - {rel}")
        if not args.yes:
            ans = input("  确认删除？输入 yes 继续：").strip().lower()
            if ans != "yes":
                print("  已取消删除（归档已生成，明文保留）。")
                return 0
        for p, rel, _ in items:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            print(f"    × 已抹除 {rel}")
        print("  ✓ 明文已抹除。要用时跑：secure\\unseal.bat")

    return 0


if __name__ == "__main__":
    sys.exit(main())
