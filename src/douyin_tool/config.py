# -*- coding: utf-8 -*-
"""配置管理模块。

负责：
- 定位「应用根目录」与「运行时数据目录」（兼容 PyInstaller 打包后的冻结环境）
- 读写 config.yaml（Cookie、下载目录、代理、批量参数）
- 记录增量下载历史（已下载的作品 ID）

两级目录概念：

1. ``get_app_dir()``   —— 程序根目录。冻结态 = exe 同级；开发态 = 项目根。
2. ``get_data_dir()``  —— 运行时数据目录。存放**会变**的东西：
   config.yaml / download_history.json / browser_profile/ / Download/ /
   logs/ / douyin_users.db。

   冻结态两者**默认**相同（exe 同级），保证老用户的数据位置一字不动；
   但可以用下面两种方式把数据目录挪走（见 `_redirected_data_dir`）：
   - 环境变量 `DOUYIN_DATA_DIR`
   - exe 同级的 `data_dir.txt`（内容写目标路径，相对路径按 exe 目录解析）

   有了这个开关，`dist/` 就能只放构建产物，运行时数据统一收在 `<项目根>/data/`。
   开发态则把数据统一收进 ``<项目根>/data/``，避免源码目录越用越乱。

打包注意：PyInstaller 冻结后，代码在临时目录 `_MEIPASS` 里，
但配置和下载产物必须放在 **exe 同级目录**，否则用户重启程序数据就丢了。
"""

from __future__ import annotations

import json
import os
import sys
import inspect
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from .storage import PersistenceError, atomic_write, locked_file


def _transaction(config_file=False):
    """把完整的读改写操作放在同一个锁内，包括其嵌套的读/写。"""
    def decorate(func):
        signature = inspect.signature(func)

        @wraps(func)
        def wrapped(*args, **kwargs):
            if config_file:
                target = get_config_path()
            else:
                target = signature.bind(*args, **kwargs).arguments.get("path") or get_history_path()
            with locked_file(target):
                return func(*args, **kwargs)
        return wrapped
    return decorate

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    # 抖音登录 Cookie（由「自动登录」写入，必需，否则大部分接口无数据）
    "cookie": "",
    # Cookie 最近一次更新时间戳（秒），用于提示是否过期
    "cookie_updated_at": 0,
    # 下载根目录（相对路径则相对于应用根目录）
    "download_dir": "Download",
    # 代理，例如 "http://127.0.0.1:7890"；留空则不用
    "proxy": "",
    # 文件名模板（f2 语法）
    "naming": "{create}_{nickname}_{desc}",
    # 主页批量默认抓取条数，0 表示不限
    "default_max_counts": 20,
    # 网络超时（秒）与重试次数
    "timeout": 10,
    "max_retries": 5,
    # Web 服务端口，0 表示随机可用端口
    "web_port": 0,
}

CONFIG_FILENAME = "config.yaml"
HISTORY_FILENAME = "download_history.json"

# 开发态存放运行时数据的子目录名（冻结态不启用，见 get_data_dir）
DATA_DIRNAME = "data"

# 冻结态数据目录重定向（仅开发/测试用；最终用户的分享包里不含这两样，
# 所以拿到的程序行为与历史版本完全一致 —— 数据就在 exe 旁边）
DATA_DIR_ENV = "DOUYIN_DATA_DIR"
DATA_POINTER_FILENAME = "data_dir.txt"

# 下载历史的结构版本。
# v1（历史遗留）：{"one": ["<aweme_id>", ...]}          —— 只存 ID，无法校验文件是否存在
# v2（当前）：    {"version": 2, "one": {"<aweme_id>": "<相对下载根目录的路径>"}}
# v1 的条目在读取时会被丢弃（视为「无法校验」），下一轮下载会重新校验一遍，
# 文件还在的会被 f2 秒跳过、不重下字节，被删过的则真正重新下载 —— 从而自愈。
HISTORY_VERSION = 2


def _warn(msg: str) -> None:
    """安全提示：延迟导入 console，避免循环依赖。

    config 被 console 反向引用（console._log_path -> config.get_data_dir），
    顶层 import 会成环，所以这里在函数内部导入。
    """
    try:
        from douyin_tool.console import safe_print
        safe_print(msg, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 路径解析（打包兼容的核心）
# ---------------------------------------------------------------------------
def get_app_dir() -> Path:
    """返回应用根目录。

    - 开发环境：项目根目录（src 的上一级）
    - PyInstaller 冻结环境：exe 所在目录

    注意：这里刻意不用 `sys._MEIPASS`，因为那是解压临时目录，
    程序退出后会被清理，不能用于存放用户配置和下载文件。
    """
    if getattr(sys, "frozen", False):
        # 冻结态：sys.executable 指向 exe 本体
        return Path(sys.executable).resolve().parent

    # 开发态：本文件位于 <root>/src/douyin_tool/config.py
    return Path(__file__).resolve().parents[2]


def _redirected_data_dir(base: Path) -> Optional[Path]:
    """冻结态下读取「数据目录重定向」配置；没配就返回 None。

    优先级：环境变量 ``DOUYIN_DATA_DIR`` > exe 同级的 ``data_dir.txt``。
    两者都只存在于开发/测试链路，对外分享包里一个都没有 ——
    所以接收方拿到的仍是「数据就在 exe 旁边」的原始行为，兼容性零风险。
    """
    env = os.environ.get(DATA_DIR_ENV, "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    pointer = base / DATA_POINTER_FILENAME
    if pointer.is_file():
        try:
            raw = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        # 空文件 / 纯注释 → 视为「未配置」，保持默认行为
        if raw and not raw.startswith("#"):
            p = Path(raw).expanduser()
            return p if p.is_absolute() else (base / p).resolve()
    return None


def get_data_dir() -> Path:
    """返回**运行时数据目录** —— 所有会变的东西都放这里。

    - 冻结态：**默认**等同于 `get_app_dir()`（exe 同级）。
      刻意不额外套一层 `data/`：老用户的 Cookie、登录态、
      下载记录、已下载文件都在 exe 旁边，保持原样才不会「数据凭空消失」。
      若 exe 同级放了 `data_dir.txt`（或设了环境变量 `DOUYIN_DATA_DIR`），
      则改为指向那里 —— 这样 `dist/` 能只放构建产物，数据统一收进 `data/`。
    - 开发态：`<项目根>/data/`。源码目录里只留工程文件，运行时产物全部下沉，
      想看自己下载了什么、Cookie 存在哪，进 `data/` 一目了然。

    目录不存在会自动创建；创建失败则退回程序根目录，保证不阻断启动。
    """
    base = get_app_dir()
    env_dir = os.environ.get(DATA_DIR_ENV, "").strip()
    if env_dir:
        p = Path(env_dir).expanduser()
        target = p if p.is_absolute() else (base / p).resolve()
    elif getattr(sys, "frozen", False):
        target = _redirected_data_dir(base) or base
    else:
        target = base / DATA_DIRNAME

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        if target != base:
            _warn(f"[警告] 数据目录不可用，已退回程序目录: {target}")
        return base
    return target


def get_resource_dir() -> Path:
    """返回应用包内的只读资源目录。

    冻结态下资源位于 ``_MEIPASS/douyin_tool``，开发态位于当前包目录。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", get_app_dir())) / "douyin_tool"
    return Path(__file__).resolve().parent


def get_config_path() -> Path:
    return get_data_dir() / CONFIG_FILENAME


def get_history_path() -> Path:
    return get_data_dir() / HISTORY_FILENAME


def resolve_download_dir(cfg: Dict[str, Any]) -> Path:
    """把配置里的下载目录解析成绝对路径，并确保存在。

    相对路径基于**数据目录**解析（开发态 = `<项目根>/data/`，
    冻结态 = exe 同级），因此默认的 `Download` 会落在数据目录内。
    """
    raw = cfg.get("download_dir") or "Download"
    p = Path(raw)
    if not p.is_absolute():
        p = get_data_dir() / p
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
@_transaction(config_file=True)
def load_config() -> Dict[str, Any]:
    """读取配置；文件不存在时用默认值创建一份。"""
    path = get_config_path()
    cfg = dict(DEFAULT_CONFIG)

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (yaml.YAMLError, OSError) as e:
            # 配置损坏时不阻断启动，退回默认值并提示
            _warn(f"[警告] 配置文件读取失败，使用默认配置: {e}")
    else:
        save_config(cfg)

    return cfg


@_transaction(config_file=True)
def save_config(cfg: Dict[str, Any]) -> None:
    """写回配置文件。"""
    path = get_config_path()
    atomic_write(path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))


@_transaction(config_file=True)
def update_config(**kwargs: Any) -> Dict[str, Any]:
    """更新部分配置项并落盘，返回更新后的完整配置。"""
    cfg = load_config()
    cfg.update(kwargs)
    save_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# 增量下载历史
# ---------------------------------------------------------------------------
# v2 结构：{"version": 2, "one": {"<aweme_id>": "<相对下载根目录的路径>"}}
#
# ★ 核心语义：「已下载」的判定标准是 **记录里的文件此刻真的还在磁盘上**。
#   用户手动删掉 mp4 之后，下次会重新下载，而不是被误判成「已下载过」。
ScopeMap = Dict[str, Dict[str, str]]


def _normalize_history(data: Any) -> ScopeMap:
    """把任意版本的历史数据归一化成 v2。

    - v2（scope 的值是 dict）：逐条保留
    - v1（scope 的值是 list）：只存了 ID、没有文件路径，**无法校验文件是否存在**
      → 整段丢弃。丢弃后下一轮会重新走一遍流程：文件还在的会被 f2 秒跳过
      （不重传字节），被删过的则真正重新下载 —— 相当于顺手做了一次自愈。
    """
    if not isinstance(data, dict):
        return {}
    result: ScopeMap = {}
    for scope, bucket in data.items():
        if scope == "version" or not isinstance(scope, str):
            continue
        if not isinstance(bucket, dict):
            # v1 的 list（或其它脏数据）→ 丢弃
            continue
        clean = {
            str(k): str(v)
            for k, v in bucket.items()
            if isinstance(v, str) and v
        }
        if clean:
            result[scope] = clean
    return result


@_transaction()
def load_history(path: Optional[Path] = None) -> ScopeMap:
    """读取下载历史（已归一化为 v2）。

    path 仅用于测试注入；生产调用不传，走 get_history_path()。
    """
    target = path or get_history_path()
    if not target.exists():
        return {}
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return _normalize_history(data)


@_transaction()
def save_history(history: Mapping[str, Mapping[str, str]],
                 path: Optional[Path] = None) -> None:
    """写回下载历史（统一 v2 格式）。"""
    target = path or get_history_path()
    payload: Dict[str, Any] = {"version": HISTORY_VERSION}
    for scope, bucket in (history or {}).items():
        if scope == "version" or not isinstance(bucket, Mapping):
            continue
        payload[scope] = {str(k): str(v) for k, v in bucket.items()}
    atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_recorded(download_dir: Path, rel: str) -> Path:
    """把记录里的路径还原成绝对路径（相对路径按下载根目录解析）。"""
    p = Path(rel)
    return p if p.is_absolute() else (Path(download_dir) / p)


def history_status(download_dir: Path, scope: str, aweme_id: str,
                   path: Optional[Path] = None) -> str:
    """查某个作品在指定作用域下的落地状态。

    返回：
      ``"unrecorded"`` 从没下过
      ``"ok"``         下过，且**文件此刻确实还在下载目录里**
      ``"stale"``      下过，但文件已经被删掉/移走了
    """
    rel = (load_history(path).get(scope) or {}).get(str(aweme_id))
    if not rel:
        return "unrecorded"
    try:
        if _resolve_recorded(download_dir, rel).is_file():
            return "ok"
    except OSError:
        pass
    return "stale"


def is_downloaded(download_dir: Path, scope: str, aweme_id: str,
                  path: Optional[Path] = None) -> bool:
    """只有「下过 **且** 文件仍在磁盘上」才算已下载。"""
    return history_status(download_dir, scope, aweme_id, path) == "ok"


def recorded_path(download_dir: Path, scope: str, aweme_id: str,
                  path: Optional[Path] = None) -> Optional[str]:
    """返回记录在案的文件绝对路径（即使文件已丢失也返回，便于提示）。"""
    rel = (load_history(path).get(scope) or {}).get(str(aweme_id))
    if not rel:
        return None
    return str(_resolve_recorded(download_dir, rel))


@_transaction()
def mark_downloaded(download_dir: Path, scope: str,
                    items: "Mapping[str, str] | Iterable[str]",
                    path: Optional[Path] = None) -> None:
    """把「作品 ID → 落盘文件」记入下载历史。

    items 推荐传 ``{aweme_id: 文件绝对路径}``；为兼容旧调用也接受
    ``[aweme_id, ...]``，但那种形式没有文件路径、**无法校验**，会被跳过不记
    （记了就会重现「文件删了还说已下载」的老问题）。

    落盘时一律转成**相对下载根目录**的路径，换目录/换机器后依然可校验。
    """
    if isinstance(items, Mapping):
        pairs: List[Tuple[str, Optional[str]]] = [
            (str(k), (str(v) if v else None)) for k, v in items.items()
        ]
    else:
        pairs = [(str(x), None) for x in items]

    pairs = [p for p in pairs if p[0]]
    if not pairs:
        return

    root = Path(download_dir)
    history = load_history(path)
    bucket = dict(history.get(scope) or {})
    for aweme_id, abs_path in pairs:
        if not abs_path:
            # 没有文件路径 → 无法校验，不记（否则会重现「删了还说下过」）
            continue
        try:
            rel = Path(abs_path).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = Path(abs_path).name      # 不在下载根目录内 → 退化成文件名
        bucket[aweme_id] = rel
    if bucket:
        history[scope] = bucket
    save_history(history, path)


@_transaction()
def reset_history(scope: Optional[str] = None,
                  path: Optional[Path] = None) -> None:
    """清空下载历史；scope 为 None 时清空全部。"""
    if scope is None:
        save_history({}, path)
        return
    history = load_history(path)
    history.pop(scope, None)
    save_history(history, path)
