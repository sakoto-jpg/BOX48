"""日历服务使用的分团配置加载（读取 /config/groups.yaml）。"""

import os
import time

import yaml

RUNTIME_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")

# 与 backend/app/group_config.py 保持一致：按 mtime 缓存 + 容忍「文件正在替换」
_CACHE = {"path": None, "mtime": None, "data": {}}


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load(path: str = None) -> dict:
    path = path or RUNTIME_PATH
    if not os.path.exists(path):
        return _CACHE["data"] if _CACHE["path"] == path else {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _CACHE["data"] if _CACHE["path"] == path else {}
    if _CACHE["path"] == path and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    try:
        data = _read(path)
    except (OSError, yaml.YAMLError):
        time.sleep(0.2)
        try:
            data = _read(path)
        except (OSError, yaml.YAMLError):
            return _CACHE["data"] if _CACHE["path"] == path else {}
    _CACHE.update(path=path, mtime=mtime, data=data)
    return data


def groups(path: str = None) -> list:
    return load(path).get("groups", []) or []


def enabled_groups(path: str = None) -> list:
    return [g for g in groups(path) if g.get("enabled", False)]
