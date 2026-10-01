#!/usr/bin/env python3
"""一键同步 Fandom wiki 数据：花名册 → 成员任职区间 → 剧目名单。

三个子任务顺序执行，任何一个失败都会中止并返回非零退出码；
写入前会备份会被覆盖的配置文件，全部成功后通知后端重新加载配置。

用法：
  python tools/sync_wiki_all.py             # 全部 dry-run（只打印差异）
  python tools/sync_wiki_all.py --apply     # 写入 roster.yaml / 数据库 / 剧目名单
  python tools/sync_wiki_all.py --apply --all-groups
  python tools/sync_wiki_all.py --apply --refresh-cache
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
import urllib.request

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
BACKUP_KEEP = 10
BACKUP_FILES = ("roster.yaml", "performances.wiki.yaml")

STEPS = (
    # (标签, 脚本, 支持的附加参数)
    ("花名册", "sync_roster_from_wiki.py", {"--apply", "--refresh-cache"}),
    ("任职区间", "sync_member_terms_from_wiki.py",
     {"--apply", "--refresh-cache", "--all-groups"}),
    ("剧目名单", "sync_performances_from_wiki.py",
     {"--apply", "--refresh-cache", "--all-groups"}),
)
RELOAD_URL = os.environ.get("RELOAD_URL", "http://127.0.0.1:8000/api/reload_metadata")


def backup_configs():
    """同步前备份会被覆盖的配置文件，只保留最近 BACKUP_KEEP 份。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name in BACKUP_FILES:
        source = os.path.join(CONFIG_DIR, name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(BACKUP_DIR, f"{name}.{stamp}.bak"))
    for name in BACKUP_FILES:
        history = sorted(glob.glob(os.path.join(BACKUP_DIR, f"{name}.*.bak")))
        for stale in history[:-BACKUP_KEEP]:
            os.remove(stale)
    print(f"[backup] 已备份 {', '.join(BACKUP_FILES)} → {BACKUP_DIR}", flush=True)


def reload_backend():
    request = urllib.request.Request(RELOAD_URL, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            print(f"[reload] {resp.status} {resp.read().decode('utf-8', 'ignore')}", flush=True)
    except Exception as exc:
        print(f"[reload] 失败（后端沿用启动时的配置）: {exc}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="同步 wiki 花名册/任职区间/剧目名单")
    parser.add_argument("--apply", action="store_true", help="写入配置与数据库")
    parser.add_argument("--refresh-cache", action="store_true", help="忽略本地缓存")
    parser.add_argument("--all-groups", action="store_true", help="包含未启用分团")
    args = parser.parse_args()

    requested = []
    if args.apply:
        requested.append("--apply")
    if args.refresh_cache:
        requested.append("--refresh-cache")
    if args.all_groups:
        requested.append("--all-groups")

    if args.apply:
        backup_configs()

    for label, script, supported in STEPS:
        extra = [flag for flag in requested if flag in supported]
        if script == "sync_roster_from_wiki.py" and args.all_groups:
            # 为未启用分团也生成 roster 条目（默认不开启自动入库）
            extra += ["--all-groups", "--create-missing"]
        print(f"\n===== {label}：{script} =====", flush=True)
        result = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, script), *extra])
        if result.returncode != 0:
            print(f"❌ {label} 同步失败（退出码 {result.returncode}），已中止", flush=True)
            return result.returncode

    if args.apply:
        reload_backend()
    print("\n✅ wiki 数据同步完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
