#!/usr/bin/env python3
"""从 Fandom wiki 同步队伍花名册（当前在册成员）。

数据来源与规则：
- groups.yaml 中每个队伍的 sources（默认 Category:<fandom>）
- 解析成员页 Infobox：simplified_chinese(中文名) / theme(分团) / team(当前队伍) /
  generation(期数) / active(在册状态) / image(照片)
- 常规队伍要求 team 字段中存在指向该队且仍开放的条目（标注 present/至今，
  或未标注日期区间）；训练生队伍以配置的分类为准
- 只更新 roster.yaml 的 members，保留 keywords / oshis / notify / enabled /
  audio_offset 等人工字段

用法：
  python tools/sync_roster_from_wiki.py                 # dry-run，输出差异
  python tools/sync_roster_from_wiki.py --refresh-cache # 忽略缓存重新抓取
  python tools/sync_roster_from_wiki.py --apply         # 写入 roster.yaml
"""

import argparse
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wiki_client import (  # noqa: E402
    category_members,
    failed_sources,
    get_member,
    load_cache,
    log,
    save_cache,
    team_still_active,
)

GROUPS_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")
ROSTER_PATH = os.environ.get("ROSTER_PATH", "/config/roster.yaml")
CACHE_PATH = os.environ.get("WIKI_MEMBER_CACHE", "/config/assets/wiki_members_cache.json")
REPORT_PATH = os.environ.get("ROSTER_SYNC_REPORT", "/config/assets/roster_sync_report.json")


def collect_team_members(team_cfg, cache, refresh):
    """返回 (成员中文名列表, 详情列表, 跳过原因)。"""
    group_name = ""
    group_code = team_cfg.get("group", "")
    fandom = team_cfg.get("fandom", "")
    sources = team_cfg.get("sources") or ([fandom] if fandom else [])
    if not sources:
        return [], [], "no_source"

    # 分团名（用于 theme 校验），从 groups.yaml 的 groups 段读取
    groups_cfg = yaml.safe_load(open(GROUPS_PATH, encoding="utf-8")) or {}
    for group in groups_cfg.get("groups", []):
        if group.get("code") == group_code:
            group_name = group.get("name", "")
            break

    pages = []
    for source in sources:
        for page in category_members(source):
            if page not in pages:
                pages.append(page)

    members = []
    details = []
    for index, page in enumerate(pages, 1):
        if index % 20 == 0:
            log(f"  抓取成员页 {index}/{len(pages)} ...")
        info = get_member(page, cache, refresh)
        if not info or not info.get("chinese"):
            continue
        if group_name and info.get("theme") != group_name:
            continue
        # 已毕业/退团的成员 active 字段是闭合区间，不含 present
        active = (info.get("active") or "").lower()
        if active and "present" not in active and "至今" not in active:
            continue
        # 要求 team 字段里存在指向目标队伍且仍在册的条目（常规队伍用队名，
        # 训练生用 SNH48 Trainee 这类标记），避免已转队或已毕业的成员混入
        team_field = info.get("team", "")
        if fandom and not team_still_active(team_field, fandom):
            continue
        if info["chinese"] in members:
            continue
        members.append(info["chinese"])
        details.append({
            "name": info["chinese"],
            "page": page,
            "group": group_code,
            "team_field": info.get("team", ""),
            "generation": info.get("generation", ""),
            "active": info.get("active", ""),
            "image": info.get("image", ""),
        })
    return members, details, ""


def main():
    parser = argparse.ArgumentParser(description="从 wiki 同步队伍花名册")
    parser.add_argument("--apply", action="store_true", help="写入 roster.yaml")
    parser.add_argument("--refresh-cache", action="store_true", help="忽略缓存重新抓取")
    parser.add_argument("--allow-partial", action="store_true",
                        help="抓取有失败时也照常写入（默认拒绝，避免覆盖成不完整名单）")
    parser.add_argument("--create-missing", action="store_true",
                        help="为已启用分团在 roster.yaml 中生成缺失的队伍条目")
    parser.add_argument("--all-groups", action="store_true",
                        help="配合 --create-missing，为未启用分团也生成条目")
    args = parser.parse_args()

    with open(ROSTER_PATH, encoding="utf-8") as fh:
        roster = yaml.safe_load(fh) or {}
    with open(GROUPS_PATH, encoding="utf-8") as fh:
        groups_cfg = yaml.safe_load(fh) or {}

    enabled_group_codes = {
        group.get("code") for group in groups_cfg.get("groups", [])
        if group.get("enabled", False)
    }
    roster_teams = {team.get("name"): team for team in roster.get("teams", [])}
    cache = load_cache(CACHE_PATH)
    report = {"teams": [], "unmapped": [], "created": []}
    group_by_code = {g.get("code"): g for g in groups_cfg.get("groups", [])}

    for team_cfg in groups_cfg.get("teams", []):
        if team_cfg.get("sync_roster", True) is False:
            continue
        name = team_cfg.get("name")
        roster_team = roster_teams.get(name)
        if roster_team is None:
            group_code = team_cfg.get("group")
            if not args.create_missing:
                if group_code in enabled_group_codes:
                    report["unmapped"].append(name)
                continue
            # 生成新条目：默认不开启自动入库，人工确认后再打开
            if group_code not in enabled_group_codes and not args.all_groups:
                continue
            new_team = {
                "name": name,
                "enabled": False,
                "keywords": list(team_cfg.get("keywords") or [name]),
                "members": [],
                "oshis": [],
                "audio_offset": 0,
                "notify": False,
            }
            roster.setdefault("teams", []).append(new_team)
            roster_teams[name] = new_team
            report["created"].append(name)
            log(f"[{name}] roster.yaml 中不存在，已生成新条目"
                f"（分团 {group_by_code.get(group_code, {}).get('name', group_code)}）")
            roster_team = new_team
        current = list(roster_team.get("members", []) or [])
        wiki_members, details, reason = collect_team_members(team_cfg, cache, args.refresh_cache)
        added = [m for m in wiki_members if m not in current]
        removed = [m for m in current if m not in wiki_members]
        report["teams"].append({
            "team": name,
            "group": team_cfg.get("group", ""),
            "current": len(current),
            "wiki": len(wiki_members),
            "added": added,
            "removed": removed,
            "reason": reason,
            "details": details,
        })
        log(f"[{name}] 现有 {len(current)} 人 / wiki {len(wiki_members)} 人"
            + (f"（{reason}）" if reason else ""))
        if added:
            log(f"   + 新增 {len(added)}: {'、'.join(added)}")
        if removed:
            log(f"   - 移除 {len(removed)}: {'、'.join(removed)}")
        if not added and not removed:
            log("   = 无变化")

    if report["unmapped"]:
        log(f"未在 groups.yaml 中映射、跳过同步的队伍: {'、'.join(report['unmapped'])}")

    parent = os.path.dirname(REPORT_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    save_cache(cache, CACHE_PATH)
    log(f"报告已写入: {REPORT_PATH}")

    if not args.apply:
        log("dry-run：未修改 roster.yaml")
        return

    failures = failed_sources()
    if failures and not args.allow_partial:
        log(f"\n⚠️ 有 {len(failures)} 个来源抓取失败，为避免覆盖成不完整名单，"
            f"本次未写入 roster.yaml。确认可接受时加 --allow-partial 重跑：")
        for item in failures:
            log(f"   - {item}")
        sys.exit(1)

    for item in report["teams"]:
        name = item["team"]
        roster_team = roster_teams[name]
        wiki_list = [d["name"] for d in item["details"]]
        current = list(roster_team.get("members", []) or [])
        # 保持现有顺序，再追加新成员；被移除的成员直接去掉
        merged = [m for m in current if m in wiki_list]
        merged += [m for m in wiki_list if m not in merged]
        roster_team["members"] = merged

    with open(ROSTER_PATH, "w", encoding="utf-8") as fh:
        yaml.dump(roster, fh, allow_unicode=True, sort_keys=False)
    log(f"已更新 roster.yaml：{len(report['teams'])} 个队伍")


if __name__ == "__main__":
    main()
