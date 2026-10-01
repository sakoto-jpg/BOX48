"""分团与队伍映射配置加载。

运行时优先读取 /config/groups.yaml（部署配置），不存在时回退到仓库内置的
backend/app/defaults/groups.yaml 模板。(group_code, team_code) 是全局唯一键。
"""

import os
import time

import yaml

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "defaults", "groups.yaml")
RUNTIME_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")

# 按 mtime 缓存：投稿同步里每条记录都会查队伍配置，不能每次都读文件；
# 同时避免读到「配置正在被替换」时的半截内容。
_CACHE = {"path": None, "mtime": None, "data": {}}


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load(path: str = None) -> dict:
    path = path or RUNTIME_PATH
    if not os.path.exists(path):
        path = DEFAULT_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _CACHE["data"] if _CACHE["path"] == path else {}
    if _CACHE["path"] == path and _CACHE["mtime"] == mtime:
        return _CACHE["data"]
    try:
        data = _read(path)
    except (OSError, yaml.YAMLError):
        # 文件可能正在被替换，稍等再读一次；仍失败则沿用上一次成功的结果
        time.sleep(0.2)
        try:
            data = _read(path)
        except (OSError, yaml.YAMLError):
            return _CACHE["data"] if _CACHE["path"] == path else {}
    _CACHE.update(path=path, mtime=mtime, data=data)
    return data


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
