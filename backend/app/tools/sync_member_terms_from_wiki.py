#!/usr/bin/env python3
"""从 Fandom wiki 同步成员任职区间到 SQLite 的 member_terms 表。

数据来源：
- groups.yaml 中每个分团的 member_sources（现役 + 毕业/转会成员分类）
- 成员页 Infobox 的 team / former_team 字段，如
  [[Team NII]] (2020-present)<br>[[Team X]] (Concurrent: 2025-2026)

产出：member_terms(member, group, team, start_year, end_year, concurrent)
每次同步整表重建，因此脚本是幂等的。

用法：
  python tools/sync_member_terms_from_wiki.py              # dry-run，只统计
  python tools/sync_member_terms_from_wiki.py --apply      # 写入数据库
  python tools/sync_member_terms_from_wiki.py --refresh-cache
  python tools/sync_member_terms_from_wiki.py --all-groups # 连未启用分团一起同步
"""

import argparse
import json
import os
import sys
from collections import defaultdict

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wiki_client import (  # noqa: E402
    category_members,
    failed_sources,
    get_member,
    load_cache,
    log,
    save_cache,
    split_team_entries,
)
from member_store import MemberStore  # noqa: E402

GROUPS_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")
DB_PATH = os.environ.get("DB_PATH", "/config/snh48.db")
CACHE_PATH = os.environ.get("WIKI_MEMBER_CACHE", "/config/assets/wiki_members_cache.json")
REPORT_PATH = os.environ.get("MEMBER_TERMS_REPORT", "/config/assets/member_terms_report.json")


def build_team_lookup(groups_cfg: dict) -> dict:
    """wiki 队伍名 → (分团代码, 队伍代码)。队伍名在全部分团中唯一。"""
    lookup = {}
    for team in groups_cfg.get("teams", []):
        # 特殊公演这类占位队伍不参与任职区间映射
        if team.get("sync_roster", True) is False:
            continue
        fandom = team.get("fandom")
        if fandom:
            lookup[fandom] = (team.get("group", ""), team.get("code", ""))
    for team in groups_cfg.get("legacy_teams", []):
        fandom = team.get("fandom")
        if fandom:
            lookup[fandom] = (team.get("group", ""), team.get("code", ""))
    return lookup


def collect_rows(group_cfg, lookup, cache, refresh, report):
    sources = group_cfg.get("member_sources") or []
    if not sources:
        return []

    pages = []
    for source in sources:
        for page in category_members(source):
            if page not in pages:
                pages.append(page)

    rows = []
    for index, page in enumerate(pages, 1):
        if index % 20 == 0:
            log(f"  抓取成员页 {index}/{len(pages)} ...")
        info = get_member(page, cache, refresh)
        if not info or not info.get("chinese"):
            continue
        groups = (
            split_team_entries(info.get("team", ""), assume_open=True)
            + split_team_entries(info.get("former_team", ""), assume_open=False)
        )
        for entry in groups:
            target = lookup.get(entry["team"])
            if not target:
                if entry["team"]:
                    report["unmapped_teams"].add(entry["team"])
                continue
            group_code, team_code = target
            for start_year, end_year in entry["ranges"]:
                rows.append({
                    "member": info["chinese"],
                    "group_code": group_code,
                    "team_code": team_code,
                    "team_raw": entry["team"],
                    "start_year": start_year,
                    "end_year": end_year,
                    "concurrent": entry["concurrent"],
                    "wiki_page": page,
                })
    return rows


def main():
    parser = argparse.ArgumentParser(description="同步成员任职区间到 member_terms 表")
    parser.add_argument("--apply", action="store_true", help="写入数据库")
    parser.add_argument("--refresh-cache", action="store_true", help="忽略缓存重新抓取")
    parser.add_argument("--all-groups", action="store_true", help="包含未启用分团")
    parser.add_argument("--allow-partial", action="store_true",
                        help="抓取有失败时也照常写入（默认拒绝，避免覆盖成不完整数据）")
    args = parser.parse_args()

    with open(GROUPS_PATH, encoding="utf-8") as fh:
        groups_cfg = yaml.safe_load(fh) or {}

    enabled = {g.get("code") for g in groups_cfg.get("groups", []) if g.get("enabled")}
    lookup = build_team_lookup(groups_cfg)
    cache = load_cache(CACHE_PATH)
    report = {"groups": [], "unmapped_teams": set()}
    all_rows = []

    for group_cfg in groups_cfg.get("groups", []):
        if not args.all_groups and group_cfg.get("code") not in enabled:
            continue
        rows = collect_rows(group_cfg, lookup, cache, args.refresh_cache, report)
        all_rows.extend(rows)
        log(f"[{group_cfg.get('name')}] 任职区间 {len(rows)} 条")
        report["groups"].append({"group": group_cfg.get("code"),
                                 "name": group_cfg.get("name"), "rows": len(rows)})

    # 去重：同一成员在同一队伍同一区间只保留一条
    unique = {}
    for row in all_rows:
        key = (row["member"], row["group_code"], row["team_code"],
               row["start_year"], row["end_year"])
        unique.setdefault(key, row)
    records = list(unique.values())

    by_team = defaultdict(set)
    for row in records:
        by_team[f"{row['group_code']}/{row['team_code']}"].add(row["member"])
    log("\n各队累计成员数（含历史）：")
    for key in sorted(by_team):
        log(f"  {key}: {len(by_team[key])} 人")
    log(f"合计 {len(records)} 条任职区间，覆盖 {len({r['member'] for r in records})} 名成员")
    if report["unmapped_teams"]:
        log(f"未映射的队伍名（已跳过）: {'、'.join(sorted(report['unmapped_teams']))}")

    report["unmapped_teams"] = sorted(report["unmapped_teams"])
    report["rows"] = len(records)
    report["members"] = len({r["member"] for r in records})
    parent = os.path.dirname(REPORT_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    save_cache(cache, CACHE_PATH)
    log(f"报告已写入: {REPORT_PATH}")

    if not args.apply:
        log("dry-run：未写入数据库")
        return

    failures = failed_sources()
    if failures and not args.allow_partial:
        log(f"\n⚠️ 有 {len(failures)} 个来源抓取失败，为避免把这些结果覆盖进数据库，"
            f"本次未写入。确认可接受时加 --allow-partial 重跑：")
        for item in failures:
            log(f"   - {item}")
        sys.exit(1)

    if not records and not args.allow_partial:
        log("\n⚠️ 本次没有抓到任何任职区间（配置或网络异常），已跳过写入以免清空数据；"
            "确实要清空时加 --allow-partial")
        sys.exit(1)

    store = MemberStore(DB_PATH)
    count = store.replace_all(records)
    log(f"已写入 member_terms：{count} 条；{store.stats()}")


if __name__ == "__main__":
    main()
