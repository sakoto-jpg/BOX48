#!/usr/bin/env python3
"""把媒体库的队伍目录改成规范命名（分团前缀 + 队伍名）。

改名前：TEAM NII / 预备生 / 特殊公演 / SNH48 GROUP / YBS ...
改名后：SNH48 TEAM NII / SNH48 预备生 / SNH48 特殊公演 / GNZ48 TEAM G ...

步骤：
  1. 逐个队伍目录计算规范名；目标不存在就改名，已存在就合并季目录
  2. 更新 downloads / orphan_files 里的路径前缀
  3. 重写每个队伍目录下 tvshow.nfo 的 title（Emby 系列名）
  4. 旧外壳（只剩海报/nfo 的目录）移动到备份目录

默认 dry-run，加 --apply 才执行。改名不复制文件，秒级完成；
执行后 Emby 需要一次全库扫描（可用 --refresh 触发）。
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/app")

import group_config  # noqa: E402
from team_utils import (  # noqa: E402
    CANON_GROUP,
    canon_from_dir,
    media_dir_name,
    parse_media_dir,
)

LIBRARY_ROOT = os.environ.get("DOWNLOAD_PATH", "/downloads")
DB_PATH = os.environ.get("DB_PATH", "/config/snh48.db")
BACKUP_ROOT = os.environ.get("MEDIA_BACKUP_ROOT", "/downloads_rename_backup")
REPORT_PATH = os.environ.get("MEDIA_RENAME_REPORT", "/config/assets/media_dir_report.json")


def log(message):
    print(message, flush=True)


def teams_by_group():
    result = {}
    for team in group_config.teams():
        result.setdefault(team.get("group"), []).append(team)
    return result


def resolve_target(dir_name: str, teams: dict):
    """返回 (目标目录名, 说明)。"""
    # 1) 已经是某个队伍的规范目录名
    for group, group_teams in teams.items():
        for team in group_teams:
            if media_dir_name(team.get("name", ""), group) == dir_name:
                return dir_name, "已是规范名"
    # 2) 能认出规范队伍名 → 换成规范目录名
    canon = canon_from_dir(dir_name)
    group_code, _ = parse_media_dir(dir_name)
    if canon and canon != CANON_GROUP:
        for team in teams.get(group_code, []):
            if canon_from_dir(team.get("name", "")) == canon:
                return media_dir_name(team.get("name", ""), group_code), f"{canon} → 规范目录"
    # 3) 群组级（特殊公演/认不出）→ 该分团的特殊公演目录
    special = next((t for t in teams.get(group_code, [])
                    if t.get("code") == "special"), None)
    if special:
        return media_dir_name(special.get("name", ""), group_code), "群组级 → 特殊公演"
    return dir_name, "无法判定，保持原样"


def merge_dirs(src_dir: str, dst_dir: str):
    """目标已存在时，把源目录的季/场次目录搬过去（同名跳过）。"""
    moved, conflicts = 0, []
    for season in sorted(os.listdir(src_dir)):
        season_src = os.path.join(src_dir, season)
        if not season.startswith("Season") or not os.path.isdir(season_src):
            continue
        season_dst = os.path.join(dst_dir, season)
        os.makedirs(season_dst, exist_ok=True)
        for name in sorted(os.listdir(season_src)):
            src_path = os.path.join(season_src, name)
            dst_path = os.path.join(season_dst, name)
            if os.path.isdir(src_path):
                if os.path.exists(dst_path):
                    conflicts.append(dst_path)
                else:
                    shutil.move(src_path, dst_path)
                    moved += 1
            elif not os.path.exists(dst_path):
                shutil.move(src_path, dst_path)   # season.nfo / poster.jpg
    return moved, conflicts


def rewrite_tvshow_title(team_dir: str, title: str) -> bool:
    path = os.path.join(team_dir, "tvshow.nfo")
    if not os.path.exists(path):
        return False
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return False
    current = root.findtext("title")
    if current == title:
        return False
    node = root.find("title")
    if node is None:
        node = ET.SubElement(root, "title")
    node.text = title
    from xml.dom import minidom
    pretty = "\n".join(
        line for line in minidom.parseString(ET.tostring(root, encoding="utf-8"))
        .toprettyxml(indent="  ").split("\n") if line.strip()
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(pretty)
    return True


def update_db_paths(db_path: str, mapping: dict) -> int:
    """把 downloads / orphan_files 里的旧目录前缀换成新目录名。"""
    updated = 0
    conn = sqlite3.connect(db_path)
    try:
        for table in ("downloads", "orphan_files"):
            try:
                for old, new in mapping.items():
                    cur = conn.execute(
                        f"UPDATE {table} SET path = REPLACE(path, ?, ?) "
                        f"WHERE path LIKE ?",
                        (f"/downloads/{old}/", f"/downloads/{new}/", f"/downloads/{old}/%"),
                    )
                    updated += cur.rowcount
            except sqlite3.OperationalError as exc:
                log(f"  ⚠️ {table} 更新失败: {exc}")
        conn.commit()
    finally:
        conn.close()
    return updated


def refresh_emby():
    """触发一次 Emby 整库刷新（改名后需要重新扫描才能更新系列）。"""
    import yaml
    from emby_client import EmbyClient
    config = yaml.safe_load(open("/config/roster.yaml", encoding="utf-8")) or {}
    emby_cfg = config.get("emby") or {}
    client = EmbyClient(emby_cfg.get("server_url", ""), emby_cfg.get("api_key", ""),
                        emby_cfg.get("library_name", ""))
    client.refresh_library()


def main():
    parser = argparse.ArgumentParser(description="媒体目录规范化迁移")
    parser.add_argument("--library-root", default=LIBRARY_ROOT)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--backup-root", default=BACKUP_ROOT)
    parser.add_argument("--apply", action="store_true", help="真正执行")
    parser.add_argument("--refresh", action="store_true", help="完成后刷新 Emby 媒体库")
    args = parser.parse_args()

    teams = teams_by_group()
    dirs = sorted(d for d in os.listdir(args.library_root)
                  if os.path.isdir(os.path.join(args.library_root, d)))
    plan = []
    for name in dirs:
        target, reason = resolve_target(name, teams)
        plan.append({"old": name, "new": target, "reason": reason,
                     "action": "keep" if target == name else "move"})

    log(f"媒体库目录 {len(dirs)} 个，需要调整 {sum(1 for p in plan if p['action'] == 'move')} 个：")
    for item in plan:
        arrow = "=" if item["action"] == "keep" else "→"
        log(f"  {item['old']:18} {arrow} {item['new']:18} ({item['reason']})")
    if not args.apply:
        log("\ndry-run：未做任何改动")
        if args.refresh:
            refresh_emby()
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(args.backup_root, stamp)
    mapping = {}
    renamed, merged, shells = 0, 0, 0
    for item in plan:
        if item["action"] == "keep":
            continue
        src = os.path.join(args.library_root, item["old"])
        dst = os.path.join(args.library_root, item["new"])
        if not os.path.exists(dst):
            os.rename(src, dst)
            renamed += 1
            log(f"  ✓ 改名 {item['old']} → {item['new']}")
        else:
            moved, conflicts = merge_dirs(src, dst)
            merged += 1
            log(f"  ✓ 合并 {item['old']} → {item['new']}（搬运 {moved} 项，"
                f"同名跳过 {len(conflicts)} 项）")
            os.makedirs(backup_dir, exist_ok=True)
            shutil.move(src, os.path.join(backup_dir, item["old"]))
            shells += 1
        mapping[item["old"]] = item["new"]

    log("\n重写 tvshow.nfo 系列名：")
    for name in sorted(os.listdir(args.library_root)):
        team_dir = os.path.join(args.library_root, name)
        if not os.path.isdir(team_dir):
            continue
        if rewrite_tvshow_title(team_dir, name):
            log(f"  ✓ {name}")

    updated = update_db_paths(args.db, mapping) if mapping else 0
    log(f"\n数据库路径更新 {updated} 条")
    if shells:
        log(f"旧目录外壳已备份到 {backup_dir}")

    report = {"stamp": stamp, "plan": plan, "mapping": mapping,
              "renamed": renamed, "merged": merged, "db_updated": updated,
              "backup_dir": backup_dir if shells else ""}
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    log(f"报告已写入: {REPORT_PATH}")

    if args.refresh:
        refresh_emby()
    return 0


if __name__ == "__main__":
    sys.exit(main())
