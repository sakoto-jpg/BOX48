"""日历服务使用的分团配置加载（读取 /config/groups.yaml）。"""

import os

import yaml

RUNTIME_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")


def load(path: str = None) -> dict:
    path = path or RUNTIME_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except OSError:
        return {}


def groups(path: str = None) -> list:
    return load(path).get("groups", []) or []


def enabled_groups(path: str = None) -> list:
    return [g for g in groups(path) if g.get("enabled", False)]
