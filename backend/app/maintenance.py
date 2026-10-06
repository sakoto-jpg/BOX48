"""维护任务：把 tools/ 下的脚本包装成可从前端触发、可查看日志的后台任务。

设计要点：
- 白名单：只有 ACTIONS 里登记的动作能被触发，参数逐个校验后拼成命令行
- 后台执行：Popen 启动，接口立刻返回 task_id，前端轮询日志
- 单任务串行：同一时间只跑一个维护任务，避免并发抓取/写库
- 破坏性动作必须两步：先 preview（脚本默认的 dry-run），再 apply

用法（由 main.py 的 /api/maintenance/* 调用）：
    task_id = start("sync_wiki", apply=True)
    status = describe(task_id)
    text = read_log(task_id, tail=4000)
"""

import os
import subprocess
import sys
import threading
import time
import uuid

TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
LOG_DIR = os.environ.get("MAINTENANCE_LOG_DIR", "/config/logs/maintenance")
MAX_KEEP = 20          # 内存里保留的任务数
MAX_TASKS = 40

# action: (脚本, 说明, 是否破坏性, 参数规格)
#   参数规格：{key: (命令行模板, 取值校验)}
ACTIONS = {
    "sync_uploads": {
        "script": "sync_uploads.py",
        "label": "同步投稿目录",
        "desc": "抓取各分团 B 站官方账号的投稿列表（慢速可续爬）",
        "destructive": False,
        "params": {
            "group": ("--group", lambda v: v in ("snh", "gnz", "bej", "ckg", "cgt")),
            "mode": ("--mode", lambda v: v in ("full", "incremental")),
            "latest": ("--latest", lambda v: str(v).isdigit() and 1 <= int(v) <= 200),
        },
    },
    "sync_wiki": {
        "script": "sync_wiki_all.py",
        "label": "同步 wiki 数据",
        "desc": "花名册 → 成员任职区间 → 剧目名单（会覆盖对应文件与数据表）",
        "destructive": True,
        "params": {
            "all_groups": ("--all-groups", lambda v: bool(v)),
        },
    },
    "rewrite_metadata": {
        "script": "rewrite_episode_metadata.py",
        "label": "重建剧集演职人员",
        "desc": "按当前匹配规则重算已有剧集的 .nfo 演员表（自动备份原文件）",
        "destructive": True,
        "params": {
            "team": ("--team", lambda v: isinstance(v, str) and len(v) <= 40),
            "bvid": ("--bvid", lambda v: isinstance(v, str) and v.startswith("BV")),
            "limit": ("--limit", lambda v: str(v).isdigit() and 1 <= int(v) <= 2000),
            "include_fallbacks": ("--include-fallbacks", lambda v: bool(v)),
        },
    },
    "repair_chapters": {
        "script": "repair_chapters.py",
        "label": "修复章节标题",
        "desc": "修复历史 mkv 的章节名（只改章节，不重新编码）",
        "destructive": True,
        "params": {},
    },
    "emby_artwork": {
        "script": "emby_artwork.py",
        "label": "刷新 Emby 素材",
        "desc": "补齐剧集封面 / 队伍海报 / 成员头像",
        "destructive": False,
        "params": {
            "only": ("--only", lambda v: v in ("people", "episodes", "teams", "refresh", "all")),
            "limit": ("--limit", lambda v: str(v).isdigit() and 1 <= int(v) <= 2000),
        },
    },
}

_tasks = {}
_order = []
_lock = threading.Lock()
_running = {"task_id": None}

# 这些参数是「存在即为真」的开关，拼命令行时不带取值
BOOLEAN_PARAMS = {"all_groups", "include_fallbacks"}


def _log_path(task_id: str) -> str:
    return os.path.join(LOG_DIR, f"{task_id}.log")


def build_command(action: str, params: dict = None, apply: bool = False) -> list:
    """把动作与参数拼成命令行；非法参数直接抛 ValueError。"""
    spec = ACTIONS.get(action)
    if not spec:
        raise ValueError(f"未知动作: {action}")
    cmd = [sys.executable, "-u", os.path.join(TOOLS_DIR, spec["script"])]
    for key, value in (params or {}).items():
        if value is None or value == "" or value is False:
            continue
        rule = spec["params"].get(key)
        if not rule:
            raise ValueError(f"{action} 不支持参数 {key}")
        flag, check = rule
        if not check(value):
            raise ValueError(f"{action} 参数 {key} 取值非法: {value}")
        cmd.append(flag)
        if key not in BOOLEAN_PARAMS:
            cmd.append(str(value))
    if apply and spec["destructive"]:
        cmd.append("--apply")      # 非破坏性动作本身就是执行态
    return cmd


def is_busy() -> bool:
    with _lock:
        return _running["task_id"] is not None


def start(action: str, params: dict = None, apply: bool = False) -> str:
    """启动一个后台维护任务，返回 task_id。"""
    spec = ACTIONS.get(action)
    if not spec:
        raise ValueError(f"未知动作: {action}")
    if is_busy():
        raise RuntimeError("已有维护任务在运行，请等待其结束")
    cmd = build_command(action, params, apply)
    task_id = f"{action}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    os.makedirs(LOG_DIR, exist_ok=True)

    record = {
        "id": task_id,
        "action": action,
        "label": spec["label"],
        "mode": "apply" if apply else "preview",
        "destructive": spec["destructive"],
        "command": " ".join(cmd),
        "status": "running",
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished": "",
        "returncode": None,
        "log": _log_path(task_id),
    }
    with _lock:
        _tasks[task_id] = record
        _order.append(task_id)
        _running["task_id"] = task_id
        while len(_order) > MAX_TASKS:
            _tasks.pop(_order.pop(0), None)

    def _run():
        try:
            with open(record["log"], "w", encoding="utf-8") as fh:
                fh.write(f"$ {' '.join(cmd)}\n\n")
                fh.flush()
                proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                        cwd=os.path.dirname(TOOLS_DIR), text=True)
                code = proc.wait()
            record["returncode"] = code
            record["status"] = "success" if code == 0 else "failed"
        except Exception as exc:                     # noqa: BLE001
            record["status"] = "failed"
            record["returncode"] = -1
            with open(record["log"], "a", encoding="utf-8") as fh:
                fh.write(f"\n启动失败: {exc}\n")
        finally:
            record["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with _lock:
                _running["task_id"] = None

    threading.Thread(target=_run, daemon=True).start()
    return task_id


def describe(task_id: str) -> dict:
    record = _tasks.get(task_id)
    return dict(record) if record else {}


def recent(limit: int = MAX_KEEP) -> list:
    with _lock:
        ids = list(_order)[-limit:]
    return [dict(_tasks[i]) for i in reversed(ids) if i in _tasks]


def read_log(task_id: str, tail: int = 6000) -> str:
    path = _log_path(task_id)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    return text[-tail:] if tail and len(text) > tail else text
