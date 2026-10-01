#!/usr/bin/env python3
"""Fandom wiki 访问与成员页解析的公共工具。

供以下工具共用：
- sync_roster_from_wiki.py       花名册（当前队员）
- sync_member_terms_from_wiki.py 任职区间（历史阵容）
- sync_performances_from_wiki.py 剧目名单

主要能力：
- wiki_api / category_members：带重试与限速的 API 访问
- parse_member_page：解析成员 Infobox，team / former_team 支持跨行
- split_team_entries：把队伍字段拆成「队伍 + 时间区间」条目
- get_member / load_cache / save_cache：带 TTL 的本地页面缓存
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

API = "https://snh48.fandom.com/api.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CACHE_PATH = os.environ.get("WIKI_MEMBER_CACHE", "/config/assets/wiki_members_cache.json")
CACHE_TTL = 7 * 24 * 3600
# 解析逻辑变更时递增，旧版本缓存会按新规则重新抓取
CACHE_VERSION = 2
# 本轮抓取失败的来源（分类或页面）。调用方在写库/写文件前应检查，
# 避免把「抓取不完整」的结果当成完整结果覆盖掉好数据。
FAILED_SOURCES = []


def record_failure(source: str, exc) -> None:
    FAILED_SOURCES.append(str(source))
    log(f"  ⚠️ {source} 抓取失败: {exc}")


def failed_sources() -> list:
    return list(dict.fromkeys(FAILED_SOURCES))

TEAM_ENTRY_RE = re.compile(r"\[\[([^\]\|]+)(?:\|([^\]]*))?\]\]")
FORMER_PREFIX_RE = re.compile(r"^(former|前)\s*", re.I)
TEAM_RANGE_RE = re.compile(r"(20\d{2})\s*[-–—~]\s*(present|至今|20\d{2})", re.I)
TEAM_YEAR_RE = re.compile(r"(20\d{2})")


def log(message):
    print(message, flush=True)


def wiki_api(params, retries=4):
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
            wait = attempt * 8
            log(f"  ⚠️ wiki 请求失败（{attempt}/{retries}）: {exc}；{wait}s 后重试")
            time.sleep(wait)


def category_members(category, limit=500):
    try:
        data = wiki_api({"action": "query", "list": "categorymembers",
                         "cmtitle": f"Category:{category}", "cmlimit": limit})
    except Exception as exc:
        # 单个分类抓取失败不应中断整轮同步；报告里该来源会是 0 条，方便发现
        record_failure(f"分类 {category}", exc)
        return []
    return [
        item["title"] for item in data.get("query", {}).get("categorymembers", [])
        if item.get("title") and not item["title"].startswith("Category:")
    ]


def clean_name(value: str) -> str:
    """处理「现用名<br>曾用名」这类字段，取第一段并去掉标签。"""
    value = re.split(r"<br\s*/?>", value or "", flags=re.I)[0]
    return re.sub(r"<[^>]+>", "", value).strip()


def canonical_team_name(value: str) -> str:
    """wiki 队伍名归一：部分成员页把历史队伍写成 `[[Former Team NIII]]`。"""
    return FORMER_PREFIX_RE.sub("", (value or "").strip()).strip()


def plain_text(value: str) -> str:
    """去掉 wiki 标记，保留可读文本（用于队伍名、日期等短字段）。"""
    value = re.sub(r"<br\s*/?>", " ", value or "", flags=re.I)
    value = re.sub(r"\[\[([^\]\|]+)\|([^\]]*)\]\]", r"\2", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def field(text, name):
    """读取模板字段（单行）。紧凑写法把多个参数挤在一行时，按 |参数= 截断。"""
    match = re.search(r"\|\s*" + re.escape(name) + r"\s*=\s*([^\n]*)", text or "")
    if not match:
        return ""
    value = match.group(1)
    cut = re.search(r"\|\s*[\w_]+\s*=", value)
    if cut:
        value = value[:cut.start()]
    return value.strip()


def raw_field(text, name):
    """捕获可能跨行的模板字段（读到下一个参数或模板结束为止）。"""
    match = re.search(r"\|\s*" + re.escape(name) + r"\s*=\s*(.*?)(?=\|\s*[\w_]+\s*=|\}\})",
                      text or "", re.S)
    return match.group(1).strip() if match else field(text, name)


def split_team_entries(team_field: str, assume_open: bool = False) -> list:
    """把 team / former_team 字段拆成条目。

    返回 [{"team": "Team NII", "ranges": [(2020, None)], "concurrent": False,
           "inactive": False, "former": False, "raw": "[[Team NII]] (2020-present)"}]
    区间用年份表示，end 为 None 表示至今；ranges 为空表示未标注时间。
    assume_open=True 时，未标注时间的条目视为「一直有效」（当前队伍字段用）。
    """
    text = team_field or ""
    entries = []
    matches = list(TEAM_ENTRY_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        tail = text[match.end():end]
        ranges = []
        concurrent = False
        inactive = False
        for chunk in re.findall(r"\(([^)]*)\)", tail):
            for piece in chunk.replace(";", ",").split(","):
                piece = piece.strip()
                if not piece:
                    continue
                if piece.lower().startswith("concurrent"):
                    concurrent = True
                    piece = piece.split(":", 1)[-1].strip()
                if "inactive" in piece.lower():
                    inactive = True
                    continue
                hit = TEAM_RANGE_RE.search(piece)
                if hit:
                    start = int(hit.group(1))
                    end_raw = hit.group(2)
                    end_year = None if end_raw.lower() in ("present", "至今") else int(end_raw)
                    ranges.append((start, end_year))
                    continue
                year = TEAM_YEAR_RE.fullmatch(piece)
                if year:
                    value = int(year.group(1))
                    ranges.append((value, value))
        if not ranges and assume_open and not inactive:
            ranges.append((None, None))
        display = (match.group(2) or "").lower()
        raw_team = match.group(1).strip()
        prefixed_former = bool(FORMER_PREFIX_RE.match(raw_team))
        entries.append({
            "team": canonical_team_name(raw_team),
            "ranges": ranges,
            "concurrent": concurrent,
            "inactive": inactive,
            "former": prefixed_former or "former" in display or "前" in display,
            "raw": text[match.start():end].strip(),
        })
    return entries


def team_still_active(team_field: str, team_name: str) -> bool:
    """判断 team 字段里是否存在仍属于 team_name 的在册条目。

    条目形态：
      [[Team SII]]                        → 未标注日期，视为在册
      [[Team SII]] (2020-present)         → 在册
      [[Team SII]] (2019–至今)            → 在册
      [[Team X]] (Concurrent: 2025-2026)  → 已结束
      [[SNH48 Trainee]] (2021-2022) (inactive) → 已退出
    """
    matched = False
    for entry in split_team_entries(team_field, assume_open=True):
        if entry["team"] != team_name:
            continue
        matched = True
        if entry["inactive"] or entry["former"]:
            continue
        if any(end is None for _, end in entry["ranges"]):
            return True
    if matched:
        return False
    # 兜底：字段没有使用 [[ ]] 链接时按纯文本判断
    if "[[" not in (team_field or "") and team_name in (team_field or ""):
        if "inactive" not in (team_field or "").lower():
            return True
    return False


def parse_member_page(text):
    name_raw = field(text, "name")
    chinese = field(text, "simplified_chinese")
    if not chinese:
        match = re.search(r"<br\s*/?>\s*([\u4e00-\u9fff·]+)", name_raw)
        chinese = match.group(1) if match else ""
    return {
        "chinese": clean_name(chinese),
        "theme": field(text, "theme"),
        "team": raw_field(text, "team"),
        "former_team": raw_field(text, "former_team"),
        "generation": field(text, "generation"),
        "active": field(text, "active"),
        "image": field(text, "image"),
    }


def load_cache(path: str = None):
    try:
        with open(path or CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache, path: str = None):
    target = path or CACHE_PATH
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


def get_member(page, cache=None, refresh=False, cache_path: str = None):
    """读取成员页（带缓存）。cache=None 时不读不写缓存。"""
    cache = cache if cache is not None else load_cache(cache_path)
    entry = cache.get(page)
    if (entry and not refresh and entry.get("v") == CACHE_VERSION
            and time.time() - entry.get("ts", 0) < CACHE_TTL):
        data = entry.get("data") or {}
        if data:
            if data.get("chinese"):
                data["chinese"] = clean_name(data["chinese"])
            return data
    try:
        data = wiki_api({"action": "parse", "page": page, "prop": "wikitext"})
    except Exception as exc:
        record_failure(f"页面 {page}", exc)
        return {}
    if not data.get("parse"):
        record_failure(f"页面 {page}", "无内容")
        return {}
    info = parse_member_page(data.get("parse", {}).get("wikitext", ""))
    if cache is not None:
        cache[page] = {"ts": time.time(), "v": CACHE_VERSION, "data": info}
    time.sleep(0.5)
    return info


def resolve_chinese_names(pages, cache=None, refresh=False, chunk=50):
    """批量把 wiki 页面标题解析成中文名（每批最多 50 个标题，单次请求）。

    未命中缓存的页面会用 action=query&prop=revisions 批量取回，
    比逐页 action=parse 快很多，同时把结果写回缓存。
    """
    cache = cache if cache is not None else load_cache()
    names = {}
    missing = []
    for page in pages:
        entry = cache.get(page)
        if (entry and not refresh and entry.get("v") == CACHE_VERSION
                and time.time() - entry.get("ts", 0) < CACHE_TTL):
            data = entry.get("data") or {}
            if data.get("chinese"):
                names[page] = clean_name(data["chinese"])
                continue
        missing.append(page)

    missing = list(dict.fromkeys(missing))
    for start in range(0, len(missing), chunk):
        batch = missing[start:start + chunk]
        log(f"  解析成员页 {min(start + chunk, len(missing))}/{len(missing)} ...")
        try:
            data = wiki_api({
                "action": "query", "titles": "|".join(batch),
                "prop": "revisions", "rvprop": "content", "rvslots": "main",
            })
        except Exception as exc:
            record_failure(f"批量解析 {len(batch)} 个成员页", exc)
            continue
        for item in data.get("query", {}).get("pages", []):
            title = item.get("title", "")
            revisions = item.get("revisions") or []
            content = ""
            if revisions:
                content = ((revisions[0].get("slots") or {}).get("main") or {}).get("content", "")
            info = parse_member_page(content) if content else {}
            if cache is not None:
                cache[title] = {"ts": time.time(), "v": CACHE_VERSION, "data": info}
            if info.get("chinese"):
                names[title] = info["chinese"]
        time.sleep(0.5)
    return names


def template_calls(text):
    """返回页面里引用的模板名列表（形如 NII7 Performing Members）。"""
    return re.findall(r"\{\{\s*([^|{}\n]+?)\s*\}\}", text or "")


def mbox_members(text):
    """解析 MBox 模板里的成员页标题：{{MBox|Team NII|Bai XinYu|...|image}}。"""
    members = []
    for call in re.finditer(r"\{\{\s*MBox\s*((?:\|[^|{}\n]*)+)\}\}", text or "", re.I):
        parts = [part.strip() for part in call.group(1).split("|")[1:]]
        if len(parts) >= 2 and parts[1] and parts[1] not in members:
            members.append(parts[1])
    return members
