"""分团与队伍映射配置加载。

运行时优先读取 /config/groups.yaml（部署配置），不存在时回退到仓库内置的
backend/app/defaults/groups.yaml 模板。(group_code, team_code) 是全局唯一键。
"""

import os
import yaml

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "defaults", "groups.yaml")
RUNTIME_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")


def load(path: str = None) -> dict:
    path = path or RUNTIME_PATH
    if not os.path.exists(path):
        path = DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except OSError:
        return {}


def groups(path: str = None) -> list:
    return load(path).get("groups", []) or []


def teams(path: str = None) -> list:
    return load(path).get("teams", []) or []


def enabled_groups(path: str = None) -> list:
    return [g for g in groups(path) if g.get("enabled", False)]


def group_by_code(code: str, path: str = None) -> dict:
    for group in groups(path):
        if group.get("code") == code:
            return group
    return {}


def group_by_gid(gid: int, path: str = None) -> dict:
    for group in groups(path):
        if int(group.get("api_gid", -1)) == int(gid):
            return group
    return {}


def team(group: str, code: str, path: str = None) -> dict:
    for item in teams(path):
        if item.get("group") == group and item.get("code") == code:
            return item
    return {}


def team_by_name(name: str, path: str = None) -> dict:
    for item in teams(path):
        if item.get("name") == name:
            return item
    return {}


def group_of_team_name(name: str, path: str = None) -> str:
    return team_by_name(name, path).get("group", "")
