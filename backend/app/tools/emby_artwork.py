#!/usr/bin/env python3
"""Emby 素材工具：剧集封面 / 队伍素材 / 成员头像。

在宿主机运行（需要 python3 + ffmpeg），只依赖标准库。默认 dry-run，加 --apply 才写入。

用法示例：
  python3 tools/emby_artwork.py --only people --limit 3        # 预览 3 个成员
  python3 tools/emby_artwork.py --only people --apply          # 抓取成员头像并刷新
  python3 tools/emby_artwork.py --only episodes --apply        # 安装剧集封面
  python3 tools/emby_artwork.py --only teams --apply           # 安装队伍素材
  python3 tools/emby_artwork.py --only refresh --apply         # 只刷新已有素材
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
WIKI_API = "https://snh48.fandom.com/api.php"

# 媒体库队伍目录 → Fandom 页面
TEAM_PAGES = {
    # 媒体库目录统一带分团前缀（阶段 4/5）
    "SNH48 TEAM NII": "Team NII",
    "SNH48 TEAM HII": "Team HII",
    "SNH48 TEAM SII": "Team SII",
    "SNH48 TEAM X": "Team X",
    "SNH48 预备生": "SNH48 Trainee",
    "SNH48 特殊公演": "SNH48",
    "GNZ48 TEAM G": "Team G",
    "GNZ48 TEAM NIII": "Team NIII",
    "GNZ48 TEAM Z": "Team Z",
    "GNZ48 预备生": "GNZ48 Trainee",
    "BEJ48 TEAM B": "Team B",
    "BEJ48 TEAM E": "Team E",
    "BEJ48 TEAM J": "Team J",
    "BEJ48 预备生": "BEJ48 Trainee",
    "CKG48 TEAM C": "Team C",
    "CKG48 TEAM K": "Team K",
    "CKG48 预备生": "CKG48 Trainee",
    "CGT48 TEAM CII": "Team CII",
    "CGT48 TEAM GII": "Team GII",
    "CGT48 预备生": "CGT48 Trainee",
    # 兼容旧目录名（迁移前后都能跑）
    "TEAM NII": "Team NII",
    "TEAM HII": "Team HII",
    "TEAM SII": "Team SII",
    "TEAM X": "Team X",
    "YBS": "SNH48 Trainee",
    "预备生": "SNH48 Trainee",
    "SNH48 GROUP": "SNH48",
    "特殊公演": "SNH48",
}


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def http_json(url, params=None, timeout=30, retries=3):
    """带重试的 JSON 请求；wiki 偶发断连时不再让整轮任务中断。"""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一重试
            last_error = exc
            if attempt < retries:
                time.sleep(attempt * 3)
    raise last_error


def download_file(url, dest, referer="https://snh48.fandom.com/", retries=3):
    """带重试的图片下载。"""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            with urllib.request.urlopen(request, timeout=120) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            return os.path.getsize(dest)
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一重试
            last_error = exc
            if attempt < retries:
                time.sleep(attempt * 3)
    raise last_error


def to_jpg(src, dest):
    return subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-q:v", "2", dest]
    ).returncode == 0


def wiki_search_page(name):
    data = http_json(WIKI_API, {
        "action": "query", "list": "search", "srsearch": name,
        "srlimit": 1, "format": "json", "formatversion": 2,
    })
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def wiki_page_image(page):
    data = http_json(WIKI_API, {
        "action": "query", "titles": page, "prop": "pageimages",
        "piprop": "original", "format": "json", "formatversion": 2,
    })
    pages = data.get("query", {}).get("pages", [])
    if pages:
        return (pages[0].get("original") or {}).get("source")
    return None


def wiki_file_url(file_name, thumb_width=None):
    params = {
        "action": "query", "titles": f"File:{file_name}", "prop": "imageinfo",
        "iiprop": "url", "format": "json", "formatversion": 2,
    }
    if thumb_width:
        # SVG 等矢量图用缩略图接口栅格化
        params["iiurlwidth"] = thumb_width
    data = http_json(WIKI_API, params)
    pages = data.get("query", {}).get("pages", [])
    if not pages or not pages[0].get("imageinfo"):
        return None
    info = pages[0]["imageinfo"][0]
    return info.get("thumburl") or info.get("url")


TEAM_BANNER_TOKEN = {"TEAM NII": "NII", "TEAM HII": "HII", "TEAM SII": "SII", "TEAM X": "X"}

# 部分页面主图是矢量 logo 或没有合适队照，这里显式指定
TEAM_PHOTO_OVERRIDE = {
    "SNH48 GROUP": "SNH48_Dec_2025.png",
    "特殊公演": "SNH48_Dec_2025.png",
}
TEAM_LOGO_OVERRIDE = {
    "SNH48 GROUP": ("SNH48_logo.svg", True),
    "特殊公演": ("SNH48_logo.svg", True),
}


def pick_team_logo(images):
    """优先位图 logo；只有 SVG 时返回 (名称, 需要栅格化)。"""
    raster = [i for i in images
              if "logo" in i.lower() and re.search(r"\.(png|jpe?g|webp)$", i, re.I)]
    if raster:
        return raster[0], False
    vector = [i for i in images if "logo" in i.lower() and i.lower().endswith(".svg")]
    if vector:
        return vector[0], True
    return None, False


def wiki_page_images(page):
    data = http_json(WIKI_API, {
        "action": "parse", "page": page, "prop": "images",
        "format": "json", "formatversion": 2,
    })
    return data.get("parse", {}).get("images", []) or []


def wiki_wikitext(page):
    data = http_json(WIKI_API, {
        "action": "parse", "page": page, "prop": "wikitext",
        "format": "json", "formatversion": 2,
    })
    return data.get("parse", {}).get("wikitext", "") or ""


def parse_member_infobox(text):
    """从成员页 Infobox 解析 theme(所属团体) 与 image(照片文件)。"""
    theme = re.search(r"\|\s*theme\s*=\s*([^\|\n}]+)", text or "")
    image = re.search(r"\|\s*image\s*=\s*([^\|\n}]+)", text or "")
    return (
        theme.group(1).strip() if theme else "",
        image.group(1).strip() if image else "",
    )


def resolve_member_image(name, prefer_theme="SNH48"):
    """按中文名找成员照片，优先所属团体为 SNH48 的页面，避免同名分团成员。

    返回 (page_title, image_file)。
    """
    data = http_json(WIKI_API, {
        "action": "query", "list": "search", "srsearch": name,
        "srlimit": 6, "format": "json", "formatversion": 2,
    })
    hits = [h["title"] for h in data.get("query", {}).get("search", [])]
    fallback = None
    for title in hits:
        if ":" in title:
            continue
        theme, image = parse_member_infobox(wiki_wikitext(title))
        if not image:
            continue
        if theme.upper() == prefer_theme.upper():
            return title, image
        if fallback is None:
            fallback = (title, image)
    return fallback if fallback else (None, None)


def parse_emby_config(config_path):
    """从 roster.yaml 里轻量解析 Emby 配置（避免依赖 PyYAML）。"""
    server_url, api_key, library_name = "", "", "公演直播回放"
    try:
        for line in open(config_path, encoding="utf-8"):
            stripped = line.strip()
            if stripped.startswith("server_url:"):
                server_url = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("api_key:"):
                api_key = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("library_name:"):
                library_name = stripped.split(":", 1)[1].strip()
    except OSError:
        pass
    return server_url, api_key, library_name


class Emby:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        self.headers = {"X-Emby-Token": token}

    def get(self, path, params=None):
        url = f"{self.url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path, params=None):
        url = f"{self.url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=self.headers, method="POST")
        with urllib.request.urlopen(request, timeout=120) as resp:
            return resp.status

    def library_id(self, name):
        for folder in self.get("/emby/Library/VirtualFolders"):
            if folder.get("Name") == name:
                return folder.get("ItemId")
        return None

    def items(self, parent_id, item_types, fields="Path"):
        results = []
        start = 0
        while True:
            data = self.get("/emby/Items", {
                "ParentId": parent_id, "Recursive": "true",
                "IncludeItemTypes": item_types, "Fields": fields,
                "StartIndex": start, "Limit": 200,
            })
            batch = data.get("Items", [])
            results.extend(batch)
            start += len(batch)
            if not batch or start >= data.get("TotalRecordCount", 0):
                break
        return results

    def persons(self, parent_id):
        results = []
        start = 0
        while True:
            data = self.get("/emby/Persons", {
                "ParentId": parent_id, "StartIndex": start, "Limit": 200,
            })
            batch = data.get("Items", [])
            results.extend(batch)
            start += len(batch)
            if not batch or start >= data.get("TotalRecordCount", 0):
                break
        return results

    def refresh(self, item_id):
        return self.post(f"/emby/Items/{item_id}/Refresh", {
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "FullRefresh",
            "ReplaceAllImages": "false",
        })


def people_dir(metadata_root, name):
    first = name[0] if name else "_"
    base = os.path.join(metadata_root, "people", first)
    exact = os.path.join(base, name)
    if os.path.isdir(exact):
        return exact
    if os.path.isdir(base):
        for entry in os.listdir(base):
            if entry == name or entry.startswith(name + "-"):
                return os.path.join(base, entry)
    return exact


def to_host_path(path, library_root):
    """数据库里存的是下载器容器路径 /downloads/…，本地对应媒体库根目录。"""
    if path.startswith("/downloads"):
        return library_root + path[len("/downloads"):]
    return path


def to_local_path(path, args):
    """Emby 报的是宿主机路径，容器内跑时要换算成本地路径。"""
    root = args.emby_path_root or args.library_root
    if root and path.startswith(root):
        return args.library_root + path[len(root):]
    return path


def step_people(args, emby):
    persons = emby.persons(args.emby_library_id)
    cache_path = os.path.join(os.path.dirname(args.config), "assets", "member_photos.json")
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except Exception:
        cache = {}
    log(f"Emby 库内人物: {len(persons)}")
    updated = 0
    for person in persons:
        name = person.get("Name") or ""
        if not name or name.upper().startswith("SNH48 GROUP"):
            continue
        target_dir = people_dir(args.emby_metadata, name)
        image_path = os.path.join(target_dir, "folder.jpg")
        has_image = os.path.exists(image_path) and os.path.getsize(image_path) > 5000

        page, image_file = resolve_member_image(name)
        url = wiki_file_url(image_file) if image_file else (
            wiki_page_image(page) if page else None)
        cached = cache.get(name) or {}
        needs_update = (
            args.force_people
            or not has_image
            or bool(image_file and cached.get("image") and cached.get("image") != image_file)
        )
        log(f"  {name}: 页面={page} 图片={image_file or '未找到'} "
            f"本地{'有图' if has_image else '无图'} "
            f"{'→ 更新' if needs_update else '(保持)'}")
        if url and args.apply and needs_update and updated < (args.limit or 10 ** 9):
            try:
                os.makedirs(target_dir, exist_ok=True)
                with tempfile.TemporaryDirectory() as tmp:
                    raw = os.path.join(tmp, "raw")
                    try:
                        download_file(url, raw)
                        to_jpg(raw, image_path)
                        log(f"    ✓ 已保存头像 {image_path}")
                        person["_new_image"] = True
                        updated += 1
                    except Exception as exc:
                        log(f"    ✗ 下载失败: {exc}")
            except Exception as exc:
                log(f"    ✗ 处理失败: {exc}")
        if url:
            cache[name] = {"page": page, "image": image_file, "url": url}
        if args.apply and (has_image or person.get("_new_image")):
            try:
                emby.refresh(person["Id"])
            except Exception as exc:
                log(f"    ✗ 刷新人物失败 {name}: {exc}")
        time.sleep(0.2)
    if args.apply:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=2)
    log("成员头像处理完成")


def step_episodes(args, emby):
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT d.path, u.cover FROM downloads d JOIN uploads u ON u.bvid = d.bvid "
        "WHERE d.status='ok' AND d.path IS NOT NULL AND u.cover IS NOT NULL"
    ).fetchall()
    conn.close()

    episodes = {os.path.realpath(to_local_path(item.get("Path", ""), args)): item
                for item in emby.items(args.emby_library_id, "Episode")}
    log(f"下载记录 {len(rows)} 条，Emby 剧集 {len(episodes)} 个")

    installed = 0
    for path, cover in rows:
        path = to_host_path(path, args.library_root)
        if not path or not os.path.exists(path):
            continue
        thumb = f"{os.path.splitext(path)[0]}-thumb.jpg"
        if os.path.exists(thumb) and os.path.getsize(thumb) > 5000:
            continue
        log(f"  {os.path.basename(path)[:50]} ← {cover[:60]}")
        if args.apply and installed < (args.limit or 10 ** 9):
            try:
                download_file(cover.replace("http://", "https://"), thumb,
                              referer="https://www.bilibili.com/")
                installed += 1
            except Exception as exc:
                log(f"    ✗ 封面下载失败: {exc}")
                continue
        if args.apply:
            item = episodes.get(os.path.realpath(path))
            if item:
                try:
                    emby.refresh(item["Id"])
                except Exception as exc:
                    log(f"    ✗ 刷新剧集失败: {exc}")
    log("剧集封面处理完成")


def pick_team_images(page):
    images = wiki_page_images(page)
    logo = next((i for i in images if "logo" in i.lower()), None)
    banner = next((i for i in images if "banner" in i.lower()), None)
    photos = [i for i in images
              if re.match(r"Team[_ ][A-Za-z0-9]+[_ ][A-Z][a-z]{2}[_ ]20\d{2}", i)]
    photo = sorted(photos)[-1] if photos else None
    return logo, photo, banner


def step_teams(args, emby):
    series = {os.path.basename(os.path.realpath(item.get("Path", ""))): item
              for item in emby.items(args.emby_library_id, "Series")}
    for folder in sorted(os.listdir(args.library_root)):
        if folder.startswith("."):
            continue
        team_dir = os.path.join(args.library_root, folder)
        if not os.path.isdir(team_dir):
            continue
        page = TEAM_PAGES.get(folder)
        if not page:
            log(f"  {folder}: 无对应维基页面，跳过")
            continue
        images = wiki_page_images(page)
        photo_override = TEAM_PHOTO_OVERRIDE.get(folder)
        if photo_override:
            photo_url = wiki_file_url(photo_override, thumb_width=1600)
        else:
            photo_url = wiki_page_image(page)
        if folder in TEAM_LOGO_OVERRIDE:
            logo, logo_is_svg = TEAM_LOGO_OVERRIDE[folder]
        else:
            logo, logo_is_svg = pick_team_logo(images)
        token = TEAM_BANNER_TOKEN.get(folder)
        banner = None
        if token:
            banner = next((i for i in images
                           if f"{token}" in i and "banner" in i.lower()), None)
        log(f"  {folder}: 队照={'有' if photo_url else '无'} "
            f"logo={logo}{'(SVG)' if logo_is_svg else ''} banner={banner}")
        if not args.apply:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            if photo_url:
                try:
                    raw = os.path.join(tmp, "photo")
                    download_file(photo_url, raw)
                    to_jpg(raw, os.path.join(team_dir, "poster.jpg"))
                    to_jpg(raw, os.path.join(team_dir, "fanart.jpg"))
                    # 每个 Season 目录也放一张季海报
                    for season in os.listdir(team_dir):
                        season_dir = os.path.join(team_dir, season)
                        if season.startswith("Season") and os.path.isdir(season_dir):
                            shutil.copyfile(os.path.join(team_dir, "poster.jpg"),
                                            os.path.join(season_dir, "poster.jpg"))
                except Exception as exc:
                    log(f"    ✗ 队照处理失败: {exc}")
            if logo:
                try:
                    url = wiki_file_url(logo, thumb_width=800 if logo_is_svg else None)
                    if url:
                        raw = os.path.join(tmp, "logo")
                        download_file(url, raw)
                        target = os.path.join(team_dir, "logo.png")
                        if logo_is_svg or logo.lower().endswith(".png"):
                            shutil.copyfile(raw, target)
                        else:
                            subprocess.run(
                                ["ffmpeg", "-y", "-loglevel", "error", "-i", raw, target],
                                check=False,
                            )
                        stale_jpg = os.path.join(team_dir, "logo.jpg")
                        if os.path.exists(stale_jpg):
                            os.remove(stale_jpg)
                except Exception as exc:
                    log(f"    ✗ logo 处理失败: {exc}")
            if banner:
                try:
                    url = wiki_file_url(banner)
                    if url:
                        raw = os.path.join(tmp, "banner")
                        download_file(url, raw)
                        to_jpg(raw, os.path.join(team_dir, "banner.jpg"))
                except Exception as exc:
                    log(f"    ✗ banner 处理失败: {exc}")
            else:
                stale = os.path.join(team_dir, "banner.jpg")
                if os.path.exists(stale):
                    os.remove(stale)
            item = series.get(folder)
            if item:
                try:
                    emby.refresh(item["Id"])
                except Exception as exc:
                    log(f"    ✗ 刷新剧集失败: {exc}")
            log(f"    ✓ 已处理")


def main():
    parser = argparse.ArgumentParser(description="Emby 素材工具")
    parser.add_argument("--only", choices=["people", "episodes", "teams", "refresh", "all"],
                        default="all")
    # 默认值优先取环境变量，这样同一份代码在宿主机和容器里都能直接跑：
    #   宿主机：python3 tools/emby_artwork.py           （用下面的默认值）
    #   容器内：docker exec ... python tools/emby_artwork.py
    #           （DOWNLOADS_PATH/CONFIG_PATH 等由 compose 注入）
    parser.add_argument("--library-root",
                        default=os.environ.get("LIBRARY_ROOT")
                        or os.environ.get("DOWNLOAD_PATH", "/mnt/nas/anime/video/live48"))
    parser.add_argument("--config",
                        default=os.environ.get("CONFIG_PATH",
                                               "/root/snh48-downloader/config/roster.yaml"))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", ""))
    parser.add_argument("--emby-metadata",
                        default=os.environ.get("EMBY_METADATA_PATH",
                                               "/root/emby-config/metadata"))
    parser.add_argument("--emby-path-root",
                        default=os.environ.get("EMBY_PATH_ROOT", ""),
                        help="Emby 上报的媒体库根路径（容器里跑时填宿主机路径）")
    parser.add_argument("--emby-url", default="")
    parser.add_argument("--emby-token", default="")
    parser.add_argument("--limit", type=int, default=0, help="限制处理数量(测试用)")
    parser.add_argument("--apply", action="store_true", help="写入并刷新 Emby")
    parser.add_argument("--force-people", action="store_true", help="成员头像全部重新抓取")
    args = parser.parse_args()

    if not args.db:
        args.db = os.path.join(os.path.dirname(args.config), "snh48.db")

    server_url, api_key, library_name = parse_emby_config(args.config)
    emby_url = args.emby_url or server_url or "http://192.168.1.12:8096"
    emby_token = args.emby_token or api_key
    if not emby_token:
        raise SystemExit("缺少 Emby API Key（可用 --emby-token 指定）")

    emby = Emby(emby_url, emby_token)
    library_id = emby.library_id(library_name)
    if not library_id:
        raise SystemExit(f"未找到 Emby 媒体库: {library_name}")
    args.emby_library_id = library_id
    log(f"媒体库 {library_name} id={library_id} 模式={'APPLY' if args.apply else 'DRY-RUN'}")

    if args.only in ("people", "refresh", "all"):
        step_people(args, emby)
    if args.only in ("episodes", "all"):
        step_episodes(args, emby)
    if args.only in ("teams", "all"):
        step_teams(args, emby)


if __name__ == "__main__":
    main()
