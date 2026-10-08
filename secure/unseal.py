# -*- coding: utf-8 -*-
"""凭据保险箱 · 解封脚本。

从 ``secure/vault.7z`` 把封存的凭据还原回原位，让程序能正常跑。

用法：
    python secure/unseal.py            # 交互输入密码
    python secure/unseal.py --force    # 原位已有明文时直接覆盖
    set DYD_VAULT_PASSWORD=xxx && python secure/unseal.py
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SECURE = BASE / "secure"
VAULT = SECURE / "vault.7z"
MANIFEST = SECURE / "vault.manifest.json"


def load_entries() -> list[dict]:
    if MANIFEST.is_file():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8")).get("entries", [])
        except (OSError, ValueError):
            pass
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="凭据保险箱 · 解封")
    ap.add_argument("-p", "--password", help="密码（不推荐：会进命令行历史）")
    ap.add_argument("--force", action="store_true", help="原位已存在明文时直接覆盖")
    args = ap.parse_args()

    try:
        import py7zr
    except ImportError:
        print("[错误] 缺少 py7zr，请先安装：pip install py7zr")
        return 1

    print("=" * 62)
    print("  凭据保险箱 · 解封")
    print("=" * 62)

    if not VAULT.is_file():
        print(f"  ✗ 找不到保险箱：{VAULT}")
        print("    先跑 secure\\seal.bat 封存一次。")
        return 1

    entries = load_entries()
    if MANIFEST.is_file():
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        print(f"  归档时间：{m.get('created_at')}   文件数：{m.get('file_count')}")
        print(f"  封存时删除了明文：{'是' if m.get('purged') else '否'}")
        if entries:
            print("  包含：")
            for e in entries:
                print(f"    - {e['path']}")

    conflicts = [e["path"] for e in entries if (BASE / e["path"]).exists()]
    if conflicts and not args.force:
        print("\n  ⚠ 以下位置已有明文，解封会覆盖：")
        for c in conflicts:
            print(f"    - {c}")
        print("  加 --force 强制覆盖，或先手工清理。")
        return 2

    pwd = args.password or os.environ.get("DYD_VAULT_PASSWORD") or getpass.getpass("保险箱密码: ")

    print(f"\n  解密还原 -> {BASE} ...")
    try:
        with py7zr.SevenZipFile(VAULT, "r", password=pwd) as z:
            z.extractall(path=BASE)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 解封失败：{type(e).__name__}: {e}")
        print("    （多半是密码不对，或归档损坏）")
        return 1

    print("  ✓ 已还原。程序现在可以直接跑了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
