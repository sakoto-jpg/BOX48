#!/usr/bin/env python3
"""SNH48 官方投稿目录同步工具（慢速、可断点续爬）。

流程：
1. 用 yt-dlp 枚举官方账号投稿列表，只取 BV 号，写入 upload_queue
2. 逐条调用 B 站 view 接口补标题/发布时间/时长/分P，写入 uploads
3. 每条处理完立即落库，进程中断后重跑即可从断点继续

使用示例：
  python tools/sync_uploads.py --stats                    # 查看进度
  python tools/sync_uploads.py --mode full                # 首次全量（很慢）
  python tools/sync_uploads.py --mode incremental         # 每天最新 10 条
  python tools/sync_uploads.py --mode incremental --latest 10 --detail-delay 8

默认参数按“慢速防风控”设计：详情请求平均间隔 8 秒（±4 秒抖动），
4700+ 条首次全量约需 10 小时以上，属于预期行为。
"""

import argparse
import fcntl
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import group_config  # noqa: E402
from upload_store import UploadStore  # noqa: E402

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CN_TZ = timezone(timedelta(hours=8))
RATE_LIMIT_CODES = {-799, -412, -509, -352}


def log(message: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


class RateLimited(Exception):
    pass


def load_cookies(cookie_path: str) -> dict:
    """解析 Netscape 格式 cookies.txt。"""
    cookies = {}
    if not cookie_path or not os.path.exists(cookie_path):
        return cookies
    with open(cookie_path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies


def build_session(cookie_path: str, mid: int) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": f"https://space.bilibili.com/{mid}/video",
        "Accept": "application/json, text/plain, */*",
    })
    session.cookies.update(load_cookies(cookie_path))
    return session


def enumerate_bvids(cookie_path: str, list_limit: int, list_delay: float, state: dict,
                    mid: int, label: str = ""):
    """用 yt-dlp 流式枚举投稿 BV 号，边枚举边产出。

    state 会写入 returncode / count，供调用方判断枚举是否完整成功。
    """
    space_url = f"https://space.bilibili.com/{mid}/video"
    cmd = [
        sys.executable, "-u", "-m", "yt_dlp",
        "--cookies", cookie_path,
        "--flat-playlist",
        "--lazy-playlist",
        "--print", "%(id)s",
        "--no-warnings",
        "--no-progress",
        "--sleep-requests", str(list_delay),
        space_url,
    ]
    if list_limit:
        cmd += ["--playlist-end", str(list_limit)]

    log(f"开始枚举投稿列表{label}（{'全部' if not list_limit else f'最新 {list_limit} 条'}，"
        f"mid={mid}，页面请求间隔 {list_delay}s）")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    index = 0
    tail = []
    for line in proc.stdout:
        bvid = line.strip()
        if len(bvid) >= 10 and bvid.startswith("BV"):
            index += 1
            yield bvid, index
        elif bvid:
            tail.append(bvid)
            tail[:] = tail[-8:]
    proc.wait()
    state["returncode"] = proc.returncode
    state["count"] = index
    if proc.returncode != 0:
        log(f"⚠️ yt-dlp 枚举结束但返回码为 {proc.returncode}，已枚举 {index} 条")
        for line in tail[-5:]:
            log(f"   yt-dlp: {line}")
        if index == 0:
            log("💡 412 / Request is blocked 通常表示 Cookie 已失效"
                "（cookies.txt 缺少 SESSDATA）或触发 B 站风控")


def fetch_detail(session: requests.Session, bvid: str) -> dict:
    response = session.get(
        VIEW_API, params={"bvid": bvid}, timeout=20
    )
    payload = response.json()
    code = payload.get("code")
    if code == 0:
        return payload["data"]
    message = payload.get("message", "")
    if code in RATE_LIMIT_CODES:
        raise RateLimited(f"code={code} {message}")
    raise RuntimeError(f"code={code} {message}")


def to_row(data: dict) -> dict:
    pages = data.get("pages") or []
    pubdate = data.get("pubdate")
    pub_date = (
        datetime.fromtimestamp(pubdate, CN_TZ).strftime("%Y-%m-%d")
        if pubdate else None
    )
    owner = data.get("owner") or {}
    return {
        "bvid": data.get("bvid"),
        "aid": data.get("aid"),
        "title": data.get("title") or "",
        "pubdate": pubdate,
        "pub_date": pub_date,
        "duration": data.get("duration"),
        "parts": len(pages) or data.get("videos") or 1,
        "owner_mid": owner.get("mid"),
        "owner_name": owner.get("name"),
        "cover": data.get("pic"),
        "pages": [
            {
                "cid": page.get("cid"),
                "page": page.get("page"),
                "part": page.get("part"),
                "duration": page.get("duration"),
            }
            for page in pages
        ],
    }


def process_pending(store: UploadStore, session: requests.Session, args) -> int:
    processed = 0
    consecutive_failures = 0
    low = max(0.0, args.detail_delay - args.detail_jitter)
    high = args.detail_delay + args.detail_jitter

    while True:
        if args.max_items and processed >= args.max_items:
            break
        item = store.next_pending()
        if not item:
            break
        bvid, group_code = item
        try:
            data = fetch_detail(session, bvid)
        except RateLimited as exc:
            consecutive_failures += 1
            wait = min(args.max_backoff, args.backoff * consecutive_failures)
            if not args.dry_run:
                store.record_failure(bvid, f"rate_limited: {exc}", args.max_attempts)
            log(f"⚠️ 疑似触发风控 {bvid}: {exc}，冷却 {wait}s"
                f"（连续异常 {consecutive_failures}/{args.max_consecutive_failures}）")
            if consecutive_failures >= args.max_consecutive_failures:
                log("连续异常过多，已保存进度并退出；稍后重跑同一命令即可续爬")
                break
            time.sleep(wait)
            continue
        except Exception as exc:
            consecutive_failures += 1
            status, attempts = ("pending", 0)
            if not args.dry_run:
                status, attempts = store.record_failure(bvid, str(exc), args.max_attempts)
            wait = min(args.max_backoff, args.backoff * consecutive_failures)
            log(f"❌ {bvid} 获取失败（第 {attempts} 次，{status}）: {exc}；冷却 {wait}s")
            if consecutive_failures >= args.max_consecutive_failures:
                log("连续异常过多，已保存进度并退出；稍后重跑同一命令即可续爬")
                break
            time.sleep(wait)
            continue

        row = to_row(data)
        row["group_code"] = group_code
        if not args.dry_run:
            store.upsert_upload(row)
            store.mark_done(bvid)
        processed += 1
        consecutive_failures = 0

        counts = store.counts()
        queue = counts.get("queue", {})
        remaining = queue.get("pending", 0)
        log(f"[{processed}] {bvid} {row['title'][:40]} | 已入库 {counts['uploads']}，"
            f"待抓 {remaining}，失败 {queue.get('failed', 0)}")

        time.sleep(random.uniform(low, high))

    return processed


def print_stats(store: UploadStore):
    counts = store.counts()
    queue = counts.get("queue", {})
    log(f"投稿已入库: {counts['uploads']} 条，最新发布日期: {counts['latest_pub_date']}")
    log(f"抓取队列: pending={queue.get('pending', 0)} "
        f"done={queue.get('done', 0)} failed={queue.get('failed', 0)}")
    for stat in store.group_stats():
        code = stat["group_code"]
        log(f"  [{code}] 已入库 {stat['uploads']} 条，最新 {stat['latest_pub_date'] or '-'}"
            f"；枚举完成 {store.get_state(f'{code}:enumerated_at') or '未完成'}"
            f"；全量完成 {store.get_state(f'{code}:full_sync_completed') or '未完成'}"
            f"；上次增量 {store.get_state(f'{code}:last_incremental_at') or '从未'}")


def target_groups(args) -> list:
    """返回需要同步的分团 [(code, name, mid)]。

    默认只同步 enabled=true 的分团；用 --group 显式指定时，即使未启用也会
    同步（接入前先试跑用）。groups.yaml 里 sync_uploads=false 的分团会被
    跳过（排期与花名册已接入、但投稿抓取排队中），显式 --group 可覆盖。
    """
    wanted = {code.lower() for code in (args.group or [])}
    targets = []
    for group in group_config.groups():
        code = group.get("code")
        mid = group.get("bilibili_mid")
        if not mid:
            continue
        if wanted:
            if code not in wanted:
                continue
        else:
            if not group.get("enabled"):
                continue
            if group.get("sync_uploads") is False:
                continue
        targets.append((code, group.get("name", code), int(mid)))
    return targets


def run(args) -> int:
    store = UploadStore(args.db)

    if args.stats:
        print_stats(store)
        return 0

    if args.reset_failed and not args.dry_run:
        reset = store.reset_failed()
        log(f"已重置失败记录 {reset} 条")

    targets = target_groups(args)
    if not targets:
        log("没有已启用且配置了 bilibili_mid 的分团，未做任何事"
            "（检查 groups.yaml 的 enabled 与 bilibili_mid）")
        return 0
    log(f"本次同步分团: {'、'.join(f'{name}({code})' for code, name, _ in targets)}")

    session = build_session(args.cookie, targets[0][2])
    enumeration_ok = True

    for code, name, mid in targets:
        label = f" · {name}"
        if args.mode == "full":
            done_key, enumerated_key = f"{code}:full_sync_completed", f"{code}:enumerated_at"
            if not args.force_enumerate and (
                store.get_state(done_key) or store.get_state(enumerated_key)
            ):
                log(f"[{code}] 投稿列表已枚举完成，跳过枚举")
                continue
            new_count = total = 0
            state = {}
            for bvid, index in enumerate_bvids(args.cookie, 0, args.list_delay, state, mid, label):
                total += 1
                if not args.dry_run and store.enqueue(bvid, index, code):
                    new_count += 1
                if total % 500 == 0:
                    log(f"[{code}] 枚举中：已发现 {total} 条，新入队 {new_count} 条")
            group_ok = state.get("returncode") == 0
            enumeration_ok = enumeration_ok and group_ok
            if group_ok and not args.dry_run:
                store.set_state(f"{code}:enumerated_at", datetime.now().isoformat(timespec="seconds"))
                store.set_state(f"{code}:enumerated_count", str(total))
            log(f"[{code}] 枚举完成：共 {total} 条，新入队 {new_count} 条")
        else:
            total = new_count = 0
            state = {}
            for bvid, index in enumerate_bvids(args.cookie, args.latest, args.list_delay,
                                               state, mid, label):
                total += 1
                if not args.dry_run and store.enqueue(bvid, index, code):
                    new_count += 1
            if state.get("returncode") != 0:
                enumeration_ok = False
            log(f"[{code}] 增量枚举完成：检查最新 {total} 条，新入队 {new_count} 条")

    processed = process_pending(store, session, args)
    log(f"本次处理完成 {processed} 条")

    if not args.dry_run:
        for code, _name, _mid in targets:
            if args.mode == "full":
                if enumeration_ok and not store.pending(limit=1):
                    store.set_state(f"{code}:full_sync_completed",
                                    datetime.now().isoformat(timespec="seconds"))
                    log(f"🎉 [{code}] 全量同步完成")
                elif not enumeration_ok:
                    log(f"⚠️ [{code}] 本次枚举未完整成功，暂不标记全量完成；重跑会自动继续")
            else:
                store.set_state(f"{code}:last_incremental_at",
                                datetime.now().isoformat(timespec="seconds"))
    return 0


def main():
    parser = argparse.ArgumentParser(description="SNH48 官方投稿目录同步（慢速可续爬）")
    parser.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    parser.add_argument("--db", default="/config/snh48.db")
    parser.add_argument("--cookie", default="/cookie/cookies.txt")
    parser.add_argument("--latest", type=int, default=10, help="增量模式检查的最新条数")
    parser.add_argument("--list-delay", type=float, default=3.0, help="枚举页面请求间隔(秒)")
    parser.add_argument("--detail-delay", type=float, default=8.0, help="详情请求平均间隔(秒)")
    parser.add_argument("--detail-jitter", type=float, default=4.0, help="详情间隔随机抖动(秒)")
    parser.add_argument("--backoff", type=float, default=300.0, help="失败基础冷却(秒)")
    parser.add_argument("--max-backoff", type=float, default=3600.0, help="失败最大冷却(秒)")
    parser.add_argument("--max-attempts", type=int, default=3, help="单条最大尝试次数")
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    parser.add_argument("--max-items", type=int, default=0, help="本次最多处理多少条详情(0=不限)")
    parser.add_argument("--force-enumerate", action="store_true", help="全量已完成也重新枚举")
    parser.add_argument("--reset-failed", action="store_true", help="把 failed 重新置为 pending")
    parser.add_argument("--stats", action="store_true", help="只打印进度统计")
    parser.add_argument("--group", action="append",
                        help="只同步指定分团代码（可重复，默认所有已启用分团）")
    parser.add_argument("--dry-run", action="store_true", help="不写数据库，只走流程")
    args = parser.parse_args()

    if args.stats:
        return run(args)

    lock_path = os.path.join(os.path.dirname(args.db), "sync_uploads.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("已有同步任务在运行，本次退出（避免并发抓取触发风控）")
        return 1

    try:
        return run(args)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
