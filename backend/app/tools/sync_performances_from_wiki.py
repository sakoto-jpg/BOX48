#!/usr/bin/env python3
"""从 Fandom wiki 生成剧目名单（config/performances.wiki.yaml）。

数据来源：
- Category:<Team> Stages         各队公演列表
- 公演页 Infobox stage           剧目中英文名、首演/收官日期
- 公演页引用的 Performing Members / Finale Performing Members 模板
  {{MBox|<队名>|<成员页>|<显示名>|<图片>}} → 中文名由成员页解析

产出文件是「自动生成层」，手工修正仍然写在 config/performances.yaml，
运行时手工文件优先。脚本每次全量重建，因而是幂等的。

用法：
  python tools/sync_performances_from_wiki.py              # dry-run
  python tools/sync_performances_from_wiki.py --apply      # 写出 yaml
  python tools/sync_performances_from_wiki.py --all-groups # 含未启用分团
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wiki_client import (  # noqa: E402
    category_members,
    field,
    load_cache,
    log,
    mbox_members,
    plain_text,
    resolve_chinese_names,
    save_cache,
    template_calls,
    wiki_api,
)

GROUPS_PATH = os.environ.get("GROUPS_PATH", "/config/groups.yaml")
OUT_PATH = os.environ.get("PERFORMANCES_OUT", "/config/performances.wiki.yaml")
CACHE_PATH = os.environ.get("WIKI_MEMBER_CACHE", "/config/assets/wiki_members_cache.json")
REPORT_PATH = os.environ.get("PERFORMANCES_REPORT", "/config/assets/performances_report.json")

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")


def parse_date(value: str) -> str:
    text = plain_text(value)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def stage_names(name_field: str) -> tuple:
    """返回 (中文名, 其它名列表)。name 形如 `<英文><br>中文<br><small>日文</small>`。"""
    parts = [plain_text(part) for part in re.split(r"<br\s*/?>", name_field or "")]
    parts = [part for part in parts if part]
    chinese = next((part for part in parts if CJK_RE.search(part)), "")
    others = [part for part in parts if part != chinese and len(part) > 2]
    if not chinese and parts:
        chinese = parts[0]
    return chinese, others


def keyword_variants(title: str, aliases: list) -> list:
    """关键词：剧目名 + 别名；去掉新生公演常见的 NEW 前缀作为额外关键词。"""
    keywords = [title] + list(aliases)
    for name in (title,) + tuple(aliases):
        stripped = re.sub(r"^NEW\s+", "", name, flags=re.I).strip()
        if stripped and stripped != name:
            keywords.append(stripped)
    return list(dict.fromkeys(k for k in keywords if k))


def template_members(template_name: str) -> list:
    data = wiki_api({"action": "parse", "page": f"Template:{template_name}", "prop": "wikitext"})
    text = (data.get("parse") or {}).get("wikitext", "")
    return mbox_members(text)


def stage_cast(text: str):
    """返回 (首演成员页, 收官成员页)。优先读模板，模板缺失时回退到页面内 MBox。"""
    templates = [t for t in template_calls(text) if "performing members" in t.lower()]
    premiere_tpl = [t for t in templates if "finale" not in t.lower()]
    finale_tpl = [t for t in templates if "finale" in t.lower()]

    premiere, finale = [], []
    if premiere_tpl or finale_tpl:
        for name in premiere_tpl:
            premiere += template_members(name)
        for name in finale_tpl:
            finale += template_members(name)
        return list(dict.fromkeys(premiere)), list(dict.fromkeys(finale))

    # 回退：直接扫描「Performing Members」小节
    match = re.search(r"==+\s*Performing Members\s*==+(.*?)(?=\n==[^=])", text or "", re.S)
    if match:
        premiere = mbox_members(match.group(1))
    return premiere, finale


def collect_stages(team_cfg, sources, cache, report):
    fandom = team_cfg.get("fandom", "")
    pages = []
    for source in sources:
        for page in category_members(source):
            if "(Cancelled)" not in page and page not in pages:
                pages.append(page)
    stages = []
    for page in pages:
        data = wiki_api({"action": "parse", "page": page, "prop": "wikitext"})
        text = (data.get("parse") or {}).get("wikitext", "")
        if not text:
            report["missing_pages"].append(page)
            continue
        title, others = stage_names(field(text, "name"))
        if not title:
            report["missing_titles"].append(page)
            continue
        premiere_pages, finale_pages = stage_cast(text)
        if not premiere_pages and not finale_pages:
            report["missing_cast"].append(page)
        stages.append({
            "page": page,
            "title": title,
            "aliases": others,
            "keywords": keyword_variants(title, others),
            "theme": field(text, "theme") or team_cfg.get("group", ""),
            "group": team_cfg.get("group", ""),
            "team": team_cfg.get("name", ""),
            "premiere": parse_date(field(text, "premiere")),
            "finale": parse_date(field(text, "finale")),
            "premiere_pages": premiere_pages,
            "finale_pages": finale_pages,
        })
        log(f"  [{team_cfg.get('name')}] {title}：首演 {len(premiere_pages)} 人 / "
            f"收官 {len(finale_pages)} 人")
    return stages


def main():
    parser = argparse.ArgumentParser(description="从 wiki 生成剧目名单")
    parser.add_argument("--apply", action="store_true", help="写出 performances.wiki.yaml")
    parser.add_argument("--refresh-cache", action="store_true", help="忽略缓存重新抓取")
    parser.add_argument("--all-groups", action="store_true", help="包含未启用分团")
    args = parser.parse_args()

    with open(GROUPS_PATH, encoding="utf-8") as fh:
        groups_cfg = yaml.safe_load(fh) or {}
    enabled = {g.get("code") for g in groups_cfg.get("groups", []) if g.get("enabled")}

    report = {"missing_pages": [], "missing_titles": [], "missing_cast": [], "stages": 0}
    cache = load_cache(CACHE_PATH)
    stages = []
    for team_cfg in groups_cfg.get("teams", []):
        if not args.all_groups and team_cfg.get("group") not in enabled:
            continue
        if team_cfg.get("sync_roster", True) is False:
            continue
        sources = team_cfg.get("stage_sources")
        if not sources:
            fandom = str(team_cfg.get("fandom", ""))
            if not fandom.startswith("Team "):
                continue
            sources = [f"{fandom} Stages"]
        stages += collect_stages(team_cfg, sources, cache, report)

    # 批量解析成员页标题 → 中文名
    pages = []
    for stage in stages:
        pages += stage["premiere_pages"] + stage["finale_pages"]
    log(f"\n共 {len(stages)} 个剧目，待解析成员页 {len(set(pages))} 个")
    names = resolve_chinese_names(pages, cache, args.refresh_cache)

    entries = []
    for stage in stages:
        premiere = [names.get(p) for p in stage["premiere_pages"]]
        finale = [names.get(p) for p in stage["finale_pages"]]
        premiere = [n for n in dict.fromkeys(premiere) if n]
        finale = [n for n in dict.fromkeys(finale) if n]
        members = premiere + [n for n in finale if n not in premiere]
        entries.append({
            "title": stage["title"],
            "team": stage["team"],
            "group": stage["group"],
            "keywords": list(dict.fromkeys([stage["title"]] + stage["aliases"])),
            "keywords": stage["keywords"],
            "premiere": stage["premiere"],
            "finale": stage["finale"],
            "members": members,
            "premiere_members": premiere,
            "finale_members": finale,
            "wiki_page": stage["page"],
        })

    report["stages"] = len(entries)
    report["members"] = len({m for entry in entries for m in entry["members"]})
    missing = [e["title"] for e in entries if not e["members"]]
    if missing:
        log(f"⚠️ 没有解析到成员名单的剧目: {'、'.join(missing)}")

    parent = os.path.dirname(REPORT_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    save_cache(cache, CACHE_PATH)

    log(f"\n共 {len(entries)} 个剧目，覆盖 {report['members']} 名成员")
    for entry in entries[:5]:
        log(f"  {entry['team']} {entry['title']}（{entry['premiere']}）: "
            f"{'、'.join(entry['members'][:6])} ...")
    log(f"报告已写入: {REPORT_PATH}")

    if not args.apply:
        log("dry-run：未写出 yaml")
        return

    header = (
        "# 由 tools/sync_performances_from_wiki.py 自动生成，请勿手工编辑。\n"
        "# 手工修正请写在 config/performances.yaml（运行时优先级更高）。\n"
        f"# 生成时间: {datetime.now().isoformat(timespec='seconds')}"
        "  来源: Fandom SNH48 wiki\n"
    )
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.dump({"performances": entries}, fh, allow_unicode=True, sort_keys=False)
    log(f"已写出 {OUT_PATH}：{len(entries)} 个剧目")


if __name__ == "__main__":
    main()
