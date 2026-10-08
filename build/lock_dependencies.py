"""记录当前验证环境中的依赖闭包；缺包或版本冲突时拒绝生成快照。"""

from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOTS = ("f2", "flask", "pyyaml", "pywebview", "websockets", "pyinstaller")


def main():
    pending = list(ROOTS)
    pinned = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in pinned:
            continue
        dist = metadata.distribution(name)
        pinned[name] = dist.version
        for raw in dist.requires or ():
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            version = metadata.version(req.name)
            if not req.specifier.contains(version, prereleases=True):
                raise RuntimeError(f"依赖冲突：{name} 要求 {req}，当前版本为 {version}")
            pending.append(req.name)
    target = Path(__file__).resolve().parents[1] / "requirements-lock.txt"
    text = (
        "# Windows / Python 3.13 已验证环境的运行与打包依赖快照。\n"
        "# 生成：python build/lock_dependencies.py；安装：python -m pip install -r requirements-lock.txt\n"
        "# 其他平台使用 requirements.txt；升级依赖后先跑回归测试再生成快照。\n"
    )
    text += "\n".join(f"{name}=={version}" for name, version in sorted(pinned.items())) + "\n"
    target.write_text(text, encoding="utf-8")
    print(f"已记录 {len(pinned)} 个依赖。")


if __name__ == "__main__":
    main()
