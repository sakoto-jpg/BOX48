#!/usr/bin/env python3
"""下载历史迁移工具（阶段一）。

把 config/download_history.txt 富化并导入 SQLite：
- 旧记录缺路径时，按标题/归一化标题/日期唯一定位本地文件
- 编码优先用 ffprobe 实测，探测失败才回退历史里记录的值
- 反向扫描媒体目录，找出“有文件但历史里没有”的孤儿文件
- 默认 dry-run，只输出报告；加 --apply 才写数据库（幂等，可重复执行）

容器内运行示例：
  python tools/migrate_history.py                 # 只生成报告
  python tools/migrate_history.py --no-probe      # 跳过 ffprobe，快速报告
  python tools/migrate_history.py --apply         # 按同样逻辑写入数据库
"""

import argparse
import collections
import csv
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime

VIDEO_EXT = {".mkv", ".mp4", ".ts"}
EPISODE_RE = re.compile(r"^S\d{4}E\d{4}\s*-\s*(.+)$")
PART_RE = re.compile(r"^S\d{4}E\d{4}\s*-\s*(.+?)\s*-\s*pt\d+$")
DATE8_RE = re.compile(r"20\d{6}")
DATE_DASH_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def extract_date(path: str) -> str | None:
    match = DATE8_RE.search(os.path.basename(path))
    if match:
        return match.group(0)
    match = DATE_DASH_RE.search(path or "")
    if match:
        return "".join(match.groups())
    return None


def container_to_host(path: str, root: str) -> str:
    if path.startswith("/downloads"):
        return root + path[len("/downloads"):]
    return path


def host_to_container(path: str, root: str) -> str:
    if root != "/downloads" and path.startswith(root):
        return "/downloads" + path[len(root):]
    return path


def scan_media(root: str):
    merged = []
    parts = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                continue
            full = os.path.join(dirpath, name)
            stem = os.path.splitext(name)[0]
            part_match = PART_RE.match(stem)
            if part_match:
                parts.append((full, part_match.group(1).strip()))
                continue
            episode_match = EPISODE_RE.match(stem)
            title = episode_match.group(1).strip() if episode_match else stem
            merged.append((full, title))
    return merged, parts


def build_indexes(merged, parts):
    by_title = collections.defaultdict(list)
    for path, title in merged:
        by_title[title].append(path)

    by_norm = collections.defaultdict(list)
    for title, paths in by_title.items():
        by_norm[norm(title)].extend(paths)

    by_date = collections.defaultdict(list)
    for path, _ in merged:
        date = extract_date(path)
        if date:
            by_date[date].append(path)

    partial_dates = collections.defaultdict(list)
    for path, title in parts:
        date = extract_date(path)
        if date:
            partial_dates[date].append(path)
        partial_dates[norm(title)].append(path)

    return by_title, by_norm, by_date, partial_dates


def parse_history(history_path: str):
    records = []
    with open(history_path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            records.append({
                "bvid": parts[0].strip(),
                "title": parts[1].strip() if len(parts) > 1 else "",
                "codec": parts[2].strip() if len(parts) > 2 else "",
                "path": parts[3].strip() if len(parts) > 3 else "",
                "line_no": line_no,
            })
    return records


def probe_media(path: str):
    """返回 (codec, duration, error)。ffprobe 不可用时 error 说明原因。"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return None, None, "ffprobe_not_found"
    except subprocess.TimeoutExpired:
        return None, None, "ffprobe_timeout"

    if result.returncode != 0:
        return None, None, (result.stderr or "ffprobe_error").strip()[:200]

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, None, "ffprobe_bad_json"

    codec = None
    for stream in data.get("streams") or []:
        if stream.get("codec_name"):
            codec = str(stream["codec_name"]).lower()
            break

    duration = None
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        pass
    return codec, duration, None


def pick_path(lines, by_title, by_norm, by_date, root):
    """按可信度定位最终文件，返回 (container_path, host_path, source, note)。"""
    # 1) 记录里已写路径且文件还在，最可信
    for rec in lines:
        if rec["path"]:
            host_path = container_to_host(rec["path"], root)
            if os.path.exists(host_path):
                return rec["path"], host_path, "recorded", ""

    title = next((rec["title"] for rec in lines if rec["title"]), "")
    if not title:
        return None, None, "none", "no_title"

    # 2) 标题精确匹配（必须唯一，避免把同名场次错配）
    candidates = by_title.get(title, [])
    if len(candidates) == 1:
        return host_to_container(candidates[0], root), candidates[0], "matched_title", ""
    if len(candidates) > 1:
        return None, None, "none", f"title_ambiguous:{len(candidates)}"

    # 3) 去除空白后再匹配
    candidates = by_norm.get(norm(title), [])
    if len(candidates) == 1:
        return host_to_container(candidates[0], root), candidates[0], "matched_normalized", ""
    if len(candidates) > 1:
        return None, None, "none", f"title_norm_ambiguous:{len(candidates)}"

    # 4) 日期唯一候选兜底
    date_match = DATE8_RE.search(title)
    if date_match:
        candidates = by_date.get(date_match.group(0), [])
        if len(candidates) == 1:
            return host_to_container(candidates[0], root), candidates[0], "matched_date", ""
        if len(candidates) > 1:
            return None, None, "none", f"date_ambiguous:{len(candidates)}"
        return None, None, "none", "date_no_file"

    # 记录里写了路径但文件已不在
    for rec in lines:
        if rec["path"]:
            return rec["path"], None, "recorded_missing", "recorded_path_missing"
    return None, None, "none", "no_match"


def derive_team(path: str | None):
    if not path or not path.startswith("/downloads/"):
        return None
    rest = path[len("/downloads/"):]
    if "/" not in rest:
        return None
    return rest.split("/", 1)[0]


def resolve_history(records, indexes, root, probe_enabled):
    by_title, by_norm, by_date, partial_dates = indexes
    grouped = collections.OrderedDict()
    for rec in records:
        grouped.setdefault(rec["bvid"], []).append(rec)

    resolved = []
    for bvid, lines in grouped.items():
        title = next((rec["title"] for rec in lines if rec["title"]), "")
        recorded_codec = next((rec["codec"] for rec in lines if rec["codec"]), "")
        container_path, host_path, source, note = pick_path(lines, by_title, by_norm, by_date, root)

        codec = None
        codec_source = "none"
        duration = None
        size = None
        probe_error = None
        if host_path and os.path.exists(host_path):
            size = os.path.getsize(host_path)
            if probe_enabled:
                codec, duration, probe_error = probe_media(host_path)
                if codec:
                    codec_source = "probed"
            if not codec and recorded_codec:
                codec = recorded_codec
                codec_source = "recorded"
        elif recorded_codec:
            codec = recorded_codec
            codec_source = "recorded"
            if container_path:
                codec_source = "recorded_unverified"

        status = "ok" if host_path and os.path.exists(host_path) else ""
        if not status:
            date_match = DATE8_RE.search(title)
            date = date_match.group(0) if date_match else ""
            partial_hit = bool(
                (date and date in partial_dates)
                or (title and norm(title) in partial_dates)
            )
            if partial_hit:
                status = "partial"
            elif container_path or source == "recorded_missing":
                status = "missing"
            else:
                status = "unmatched"

        perf_date = None
        date_match = DATE8_RE.search(title)
        if date_match:
            perf_date = date_match.group(0)
        elif host_path:
            perf_date = extract_date(host_path)

        resolved.append({
            "bvid": bvid,
            "title": title,
            "status": status,
            "path": container_path,
            "path_source": source,
            "codec": codec,
            "codec_source": codec_source,
            "duration": duration,
            "file_size": size,
            "team": derive_team(container_path),
            "perf_date": perf_date,
            "reason": note or probe_error or "",
            "line_count": len(lines),
        })

    return resolved


def find_orphans(merged, resolved, root):
    used_host = set()
    for item in resolved:
        if not item["path"]:
            continue
        used_host.add(item["path"])
    orphans = []
    for host_path, title in merged:
        container_path = host_to_container(host_path, root)
        if container_path not in used_host:
            orphans.append({"path": container_path, "title": title, "perf_date": extract_date(host_path)})
    return orphans


def write_report(report, report_path):
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    csv_path = os.path.splitext(report_path)[0] + ".csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "bvid", "title", "status", "path", "path_source",
            "codec", "codec_source", "duration", "file_size", "team",
            "perf_date", "reason", "line_count",
        ])
        writer.writeheader()
        writer.writerows(report["records"])
    return csv_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
  bvid TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  codec TEXT,
  path TEXT,
  status TEXT NOT NULL DEFAULT 'ok',
  path_source TEXT,
  codec_source TEXT,
  file_size INTEGER,
  duration REAL,
  team TEXT,
  perf_date TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS orphan_files (
  path TEXT PRIMARY KEY,
  title TEXT,
  perf_date TEXT,
  discovered_at TEXT
);
"""


def apply_to_db(report, db_path):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        for item in report["records"]:
            conn.execute(
                """
                INSERT INTO downloads (
                  bvid, title, codec, path, status, path_source, codec_source,
                  file_size, duration, team, perf_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                  title=excluded.title,
                  codec=excluded.codec,
                  path=excluded.path,
                  status=excluded.status,
                  path_source=excluded.path_source,
                  codec_source=excluded.codec_source,
                  file_size=excluded.file_size,
                  duration=excluded.duration,
                  team=excluded.team,
                  perf_date=excluded.perf_date,
                  updated_at=excluded.updated_at
                """,
                (
                    item["bvid"], item["title"], item["codec"], item["path"], item["status"],
                    item["path_source"], item["codec_source"], item["file_size"],
                    item["duration"], item["team"], item["perf_date"], now, now,
                ),
            )
        conn.execute("DELETE FROM orphan_files")
        for orphan in report["orphans"]:
            conn.execute(
                "INSERT OR REPLACE INTO orphan_files (path, title, perf_date, discovered_at) VALUES (?, ?, ?, ?)",
                (orphan["path"], orphan["title"], orphan["perf_date"], now),
            )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()


def main():
    parser = argparse.ArgumentParser(description="下载历史迁移与富化（默认 dry-run）")
    parser.add_argument("--history", default="/config/download_history.txt")
    parser.add_argument("--root", default="/downloads", help="媒体库根目录（容器内路径）")
    parser.add_argument("--db", default="/config/snh48.db")
    parser.add_argument("--report", default="/config/migration_report.json")
    parser.add_argument("--apply", action="store_true", help="写入 SQLite（默认只出报告）")
    parser.add_argument("--no-probe", action="store_true", help="跳过 ffprobe 编码探测")
    parser.add_argument("--keep-unmatched", action="store_true",
                        help="保留无法匹配的记录(status=unmatched)；默认丢弃且不入库")
    args = parser.parse_args()

    if not os.path.exists(args.history):
        raise SystemExit(f"历史文件不存在: {args.history}")
    if not os.path.isdir(args.root):
        raise SystemExit(f"媒体目录不存在: {args.root}")

    merged, parts = scan_media(args.root)
    indexes = build_indexes(merged, parts)
    records = parse_history(args.history)
    resolved = resolve_history(records, indexes, args.root, probe_enabled=not args.no_probe)
    dropped = []
    if not args.keep_unmatched:
        dropped = [item for item in resolved if item["status"] == "unmatched"]
        resolved = [item for item in resolved if item["status"] != "unmatched"]
    orphans = find_orphans(merged, resolved, args.root)

    summary = collections.Counter()
    summary["history_lines"] = len(records)
    summary["unique_bvid"] = len(resolved)
    summary["merged_files"] = len(merged)
    summary["partial_files"] = len(parts)
    summary["orphan_files"] = len(orphans)
    for item in resolved:
        summary[f"status_{item['status']}"] += 1
        summary[f"path_{item['path_source']}"] += 1
        summary[f"codec_{item['codec_source']}"] += 1

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "history": args.history,
        "root": args.root,
        "summary": dict(sorted(summary.items())),
        "records": resolved,
        "dropped": dropped,
        "orphans": orphans,
    }
    csv_path = write_report(report, args.report)

    print(f"历史行数: {summary['history_lines']}，去重后 BV 数: {summary['unique_bvid']}")
    for key in sorted(summary):
        if key.startswith(("status_", "path_", "codec_")):
            print(f"  {key}: {summary[key]}")
    if dropped:
        print(f"丢弃(无法匹配、不入库): {len(dropped)}")
        for item in dropped:
            print(f"  {item['bvid']} | {item['title']} | {item['reason']}")
    print(f"媒体文件: {summary['merged_files']}，分P残留: {summary['partial_files']}，孤儿文件: {summary['orphan_files']}")
    print(f"报告: {args.report}")
    print(f"      {csv_path}")

    if args.apply:
        apply_to_db(report, args.db)
        print(f"已写入数据库: {args.db}")
    else:
        print("dry-run：未写数据库。确认报告无误后加 --apply 执行。")


if __name__ == "__main__":
    main()
