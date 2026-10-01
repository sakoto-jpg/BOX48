#!/usr/bin/env python3
"""按当前匹配规则重建已有剧集的演职人员（重写媒体库中的 .nfo）。

三层匹配（剧目名单 → 历史任职区间 → 兜底）上线后，之前入库的剧集仍然挂着
下载当时的花名册。本工具遍历下载历史，逐场重算演员表并回写 NFO。

安全设计：
- 默认 dry-run，只打印「旧 → 新」差异，不写文件
- 写入前把原 NFO 备份到 config/backups/nfo/<时间戳>/ 下
- 演员表无变化时跳过，除非显式 --force

用法：
  python tools/rewrite_episode_metadata.py                     # 预览差异
  python tools/rewrite_episode_metadata.py --apply             # 重写 NFO
  python tools/rewrite_episode_metadata.py --apply --refresh   # 并刷新 Emby
  python tools/rewrite_episode_metadata.py --bvid BV1uKah6pEiq --apply
  python tools/rewrite_episode_metadata.py --team 预备生 --limit 5
"""

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from xml.dom import minidom

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/app")

from member_store import MemberStore  # noqa: E402
from nfo_builder import NfoBuilder, canon_team  # noqa: E402

DOWNLOAD_ROOT = os.environ.get("DOWNLOAD_PATH", "/downloads")
DB_PATH = os.environ.get("DB_PATH", "/config/snh48.db")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/roster.yaml")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups", "nfo")
REPORT_PATH = os.path.join(CONFIG_DIR, "assets", "nfo_rewrite_report.json")


def log(message):
    print(message, flush=True)


def find_episode_nfo(video_path: str):
    """返回 (nfo 路径, 目录)。优先匹配与视频同名的 nfo。"""
    folder = os.path.dirname(video_path)
    if not os.path.isdir(folder):
        return None, folder
    candidates = sorted(glob.glob(os.path.join(folder, "*.nfo")))
    if not candidates:
        return None, folder
    stem = os.path.splitext(os.path.basename(video_path))[0]
    for candidate in candidates:
        if os.path.splitext(os.path.basename(candidate))[0] == stem:
            return candidate, folder
    return candidates[0], folder


def read_actor_names(nfo_path: str):
    try:
        root = ET.parse(nfo_path).getroot()
    except (ET.ParseError, OSError):
        return None
    return [a.findtext("name") or "" for a in root.findall("actor")]


def write_actors(nfo_path: str, members: list, backup_target: str) -> None:
    """只替换 actor 节点，其余字段（标题/简介/日期等）原样保留。"""
    root = ET.parse(nfo_path).getroot()
    for actor in root.findall("actor"):
        root.remove(actor)
    for member in members:
        actor = ET.SubElement(root, "actor")
        ET.SubElement(actor, "name").text = member
        ET.SubElement(actor, "role").text = "Member"
    os.makedirs(os.path.dirname(backup_target), exist_ok=True)
    shutil.copy2(nfo_path, backup_target)
    serialized = ET.tostring(root, encoding="utf-8")
    pretty = "\n".join(
        line for line in minidom.parseString(serialized).toprettyxml(indent="  ").split("\n")
        if line.strip()
    )
    with open(nfo_path, "w", encoding="utf-8") as fh:
        fh.write(pretty)


def load_downloads(args):
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    query = ("SELECT bvid, title, team, perf_date, scope, path FROM downloads "
             "WHERE status = 'ok' AND path IS NOT NULL")
    params = []
    if args.bvid:
        query += " AND bvid IN (%s)" % ",".join("?" * len(args.bvid))
        params += args.bvid
    if args.team:
        query += " AND team = ?"
        params.append(args.team)
    query += " ORDER BY perf_date"
    rows = [dict(r) for r in conn.execute(query, params)]
    conn.close()
    if args.limit:
        rows = rows[:args.limit]
    return rows


def to_host_path(path: str, library_root: str) -> str:
    if library_root and path.startswith(DOWNLOAD_ROOT):
        return library_root + path[len(DOWNLOAD_ROOT):]
    return path


def refresh_emby(video_paths, args):
    """刷新 Emby：先按条目精确刷新，最后整库兜底刷新一次。"""
    if not video_paths:
        return
    import yaml
    if not os.path.exists(args.config):
        log("⚠️ 找不到配置文件，跳过 Emby 刷新")
        return
    config = yaml.safe_load(open(args.config, encoding="utf-8")) or {}
    emby_cfg = config.get("emby") or {}
    url = args.emby_url or emby_cfg.get("server_url", "")
    token = args.emby_token or emby_cfg.get("api_key", "")
    library = emby_cfg.get("library_name", "")
    if not url or not token:
        log("⚠️ 未配置 Emby，跳过刷新")
        return

    from emby_client import EmbyClient
    client = EmbyClient(url, token, library)
    library_id = client.library_id
    if not library_id:
        log(f"⚠️ 未找到 Emby 媒体库: {library}")
        return

    wanted = {to_host_path(p, args.library_root): p for p in video_paths}
    by_name = {os.path.basename(k): k for k in wanted}
    items = client.items(library_id, "Episode", fields="Path")
    log(f"[Emby] 库内剧集 {len(items)} 条，本次需要刷新 {len(wanted)} 条")

    refreshed = 0
    for item in items:
        item_path = item.get("Path") or ""
        key = wanted.get(item_path) or by_name.get(os.path.basename(item_path))
        if not key:
            continue
        if client.refresh_item(item.get("Id"), full=True):
            refreshed += 1
        time.sleep(0.1)
    log(f"[Emby] 已精确刷新 {refreshed} 条剧集")
    client.refresh_library()


def main():
    parser = argparse.ArgumentParser(description="重建已有剧集的演职人员")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--library-root", default="/mnt/nas/anime/video/live48")
    parser.add_argument("--emby-url", default="")
    parser.add_argument("--emby-token", default="")
    parser.add_argument("--bvid", action="append", help="只处理指定 BV 号（可重复）")
    parser.add_argument("--team", help="只处理指定队伍（规范名，如 预备生）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条")
    parser.add_argument("--force", action="store_true", help="演员表无变化时也重写")
    parser.add_argument("--include-fallbacks", action="store_true",
                        help="对「历史任职区间/当前花名册」兜底的结果也重写"
                             "（按年精度会带上已离队成员，默认只应用剧目名单命中）")
    parser.add_argument("--apply", action="store_true", help="写入 NFO")
    parser.add_argument("--refresh", action="store_true", help="写入后刷新 Emby")
    args = parser.parse_args()

    member_store = MemberStore(args.db)
    builder = NfoBuilder(args.config, DOWNLOAD_ROOT, member_store)
    rows = load_downloads(args)
    log(f"待处理下载记录: {len(rows)} 条（模式：{'写入' if args.apply else 'dry-run'}）\n")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = {"total": len(rows), "changed": 0, "unchanged": 0, "skipped": 0,
              "failed": 0, "items": []}
    changed_videos = []

    for row in rows:
        bvid, title = row["bvid"], row["title"] or ""
        video = row["path"]
        team = canon_team(row["team"] or "")
        date = re.sub(r"\D", "", str(row["perf_date"] or ""))
        year = date[:4] if len(date) >= 4 else ""
        if not year:
            match = re.search(r"(20\d{2})", video)
            year = match.group(1) if match else ""

        members, source = builder._members_for(title, team, year) if year else ([], "unknown")
        nfo_path, folder = find_episode_nfo(video)
        if not nfo_path:
            log(f"⚠️ {bvid} 未找到 NFO: {folder}")
            report["failed"] += 1
            continue

        old_names = read_actor_names(nfo_path)
        if old_names is None:
            log(f"⚠️ {bvid} NFO 解析失败: {nfo_path}")
            report["failed"] += 1
            continue

        same = old_names == members
        detail = {"bvid": bvid, "team": team, "date": date, "source": source,
                  "old_count": len(old_names), "new_count": len(members),
                  "changed": not same or args.force}
        report["items"].append(detail)

        # 剧目名单命中是最可靠的来源；历史任职区间按年匹配会带上已离队成员，
        # 默认只用于补全原本没有演员表的剧集。
        stage_matched = source.startswith("performances")
        if not same and not stage_matched and old_names and not args.include_fallbacks:
            report["skipped"] += 1
            detail["changed"] = False
            log(f"[{source}] {date} {team} {bvid}: 跳过（回退来源，"
                f"{len(old_names)} → {len(members)} 人；如确认可用 --include-fallbacks）")
            continue

        log(f"[{source}] {date} {team} {bvid}: "
            f"{'无变化' if same else f'{len(old_names)} → {len(members)} 人'}")
        if not same:
            log(f"    旧: {'、'.join(old_names) if old_names else '(空)'}")
            log(f"    新: {'、'.join(members) if members else '(空)'}")

        if same and not args.force:
            report["unchanged"] += 1
            continue

        report["changed"] += 1
        if args.apply:
            backup = os.path.join(BACKUP_DIR, stamp, os.path.relpath(nfo_path, "/"))
            try:
                write_actors(nfo_path, members, backup)
                changed_videos.append(video)
            except Exception as exc:
                log(f"    ✗ 写入失败: {exc}")
                report["failed"] += 1

    log(f"\n汇总：共 {report['total']} 条，需要更新 {report['changed']} 条，"
        f"无变化 {report['unchanged']} 条，跳过 {report['skipped']} 条，"
        f"失败 {report['failed']} 条")
    if args.apply:
        log(f"原 NFO 已备份到 {os.path.join(BACKUP_DIR, stamp)}")

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    log(f"报告已写入: {REPORT_PATH}")

    if not args.apply:
        log("dry-run：未修改任何 NFO")
        return 0

    if args.refresh:
        refresh_emby(changed_videos, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
