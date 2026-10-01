#!/usr/bin/env python3
"""从 Fandom wiki 同步队伍花名册（成员名单）。

数据来源与规则：
- groups.yaml 中每个队伍的 sources（默认 Category:<fandom>）
- 解析成员页 Infobox：simplified_chinese(中文名) / theme(分团) / team(当前队伍) /
  generation(期数) / active(在册状态) / image(照片)
- 常规队伍要求 team 字段包含该队并标注 present；训练生队伍以配置的分类为准
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
import re
import time
import urllib.parse
import urllib.request

import yaml

API = "https://snh48.fandom.com/api.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

GROUPS_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")
ROSTER_PATH = os.environ.get("ROSTER_PATH", "/config/roster.yaml")
CACHE_PATH = os.environ.get("WIKI_MEMBER_CACHE", "/config/assets/wiki_members_cache.json")
REPORT_PATH = os.environ.get("ROSTER_SYNC_REPORT", "/config/assets/roster_sync_report.json")
CACHE_TTL = 7 * 24 * 3600


def log(message):
    print(message, flush=True)


def wiki_api(params, retries=3):
    params = dict(params, format="json", formatversion=2)
    url = f"{API}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if attempt >= retries:
                raise
            wait = attempt * 5
            log(f"  ⚠️ wiki 请求失败（{attempt}/{retries}）: {exc}；{wait}s 后重试")
            time.sleep(wait)


def category_members(category):
    data = wiki_api({"action": "query", "list": "categorymembers",
                     "cmtitle": f"Category:{category}", "cmlimit": 500})
    return [
        item["title"] for item in data.get("query", {}).get("categorymembers", [])
        if item.get("title") and not item["title"].startswith("Category:")
    ]


def clean_name(value: str) -> str:
    """处理「现用名<br>曾用名」这类字段，取第一段并去掉标签。"""
    value = re.split(r"<br\s*/?>", value or "", flags=re.I)[0]
    return re.sub(r"<[^>]+>", "", value).strip()


def parse_member_page(text):
    def field(name):
        match = re.search(r"\|\s*" + name + r"\s*=\s*(.+)", text or "")
        return match.group(1).strip() if match else ""

    name_raw = field("name")
    chinese = field("simplified_chinese")
    if not chinese:
        match = re.search(r"<br\s*/?>\s*([\u4e00-\u9fff·]+)", name_raw)
        chinese = match.group(1) if match else ""
    chinese = clean_name(chinese)
    return {
        "chinese": chinese,
        "theme": field("theme"),
        "team": field("team"),
        "generation": field("generation"),
        "active": field("active"),
        "image": field("image"),
    }


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    parent = os.path.dirname(CACHE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


def get_member(page, cache, refresh):
    entry = cache.get(page)
    if entry and not refresh and time.time() - entry.get("ts", 0) < CACHE_TTL:
        data = entry.get("data") or {}
        # 兼容旧缓存里未清洗的姓名
        if data.get("chinese"):
            data["chinese"] = clean_name(data["chinese"])
        return data
    data = wiki_api({"action": "parse", "page": page, "prop": "wikitext"})
    info = parse_member_page(data.get("parse", {}).get("wikitext", ""))
    cache[page] = {"ts": time.time(), "data": info}
    time.sleep(0.5)
    return info


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
        # 要求 team 字段包含目标队伍（常规队伍用队名，训练生用 SNH48 Trainee 这类标记）
        # 且仍在册（present / 至今），避免已转队或已毕业的成员混入
        team_field = info.get("team", "")
        if fandom and fandom not in team_field:
            continue
        if "present" not in team_field and "至今" not in team_field:
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
    cache = load_cache()
    report = {"teams": [], "unmapped": []}

    for team_cfg in groups_cfg.get("teams", []):
        if team_cfg.get("sync_roster", True) is False:
            continue
        name = team_cfg.get("name")
        roster_team = roster_teams.get(name)
        if roster_team is None:
            if team_cfg.get("group") in enabled_group_codes:
                report["unmapped"].append(name)
            continue
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
    save_cache(cache)
    log(f"报告已写入: {REPORT_PATH}")

    if not args.apply:
        log("dry-run：未修改 roster.yaml")
        return

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
