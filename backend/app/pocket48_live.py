"""口袋48直播：直播间列表聚合 + RTMP→HLS 按需转封装。

- 列表走 getLiveList，缓存 30 秒，避免频繁请求触发风控
- 播放时用 getLiveOne 取 playStreamPath，ffmpeg `-c copy` 转 HLS（不解码）
- 无观众 90 秒后自动停止 ffmpeg 并清理分片
"""

import os
import shutil
import subprocess
import threading
import time

import requests
import yaml

LIST_URL = "https://pocketapi.48.cn/live/api/v1/live/getLiveList"
ONE_URL = "https://pocketapi.48.cn/live/api/v1/live/getLiveOne"
MEDIA_BASE = "https://source.48.cn"
HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "User-Agent": "PocketFans201807/6.0.0 (iPhone; iOS 14.4; Scale/3.00)",
    "appInfo": '{"vendor":"apple","deviceId":"0","appVersion":"6.0.0",'
               '"appBuild":"200513","osVersion":"14.4.0","osType":"ios",'
               '"deviceName":"iphone","os":"ios"}',
}

CACHE_TTL = 30
IDLE_SECONDS = 90
STREAM_ROOT = os.environ.get("POCKET_STREAM_PATH", "/config/live")

_cache = {"data": None, "ts": 0.0}
_cache_lock = threading.Lock()
_streams = {}
_streams_lock = threading.Lock()


def _post(url, payload, timeout=15):
    resp = requests.post(url, json=payload, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _media_url(path):
    if not path:
        return ""
    text = str(path)
    return text if text.startswith("http") else MEDIA_BASE + text


def _load_oshis(roster_path):
    try:
        with open(roster_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return set()
    oshis = set()
    for team in data.get("teams") or []:
        for name in team.get("oshis") or []:
            if str(name).strip():
                oshis.add(str(name).strip())
    return oshis


def _group_of(nickname, star_name):
    text = f"{nickname} {star_name}".upper()
    for code in ("SNH48", "GNZ48", "BEJ48", "CKG48", "CGT48"):
        if code in text:
            return code
    return "SNH48 GROUP"


def fetch_lives(roster_path, force=False):
    now = time.time()
    with _cache_lock:
        if (not force and _cache["data"] is not None
                and now - _cache["ts"] < CACHE_TTL):
            return _cache["data"]

    data = _post(LIST_URL, {"debug": True, "next": "0", "groupId": 0, "record": False})
    raw = (data.get("content") or {}).get("liveList") or []
    oshis = _load_oshis(roster_path)

    items = []
    for live in raw:
        user = live.get("userInfo") or {}
        nickname = user.get("nickname") or ""
        star_name = user.get("starName") or ""
        is_oshi = bool(oshis) and (
            star_name in oshis or any(name in nickname for name in oshis)
        )
        items.append({
            "live_id": str(live.get("liveId") or ""),
            "title": live.get("title") or "直播中",
            "cover_url": _media_url(live.get("coverPath")),
            "cover_width": live.get("coverWidth") or 0,
            "cover_height": live.get("coverHeight") or 0,
            "live_type": live.get("liveType") or 1,
            "start_time": int(live.get("ctime") or 0),
            "online_num": int(live.get("onlineUserNum") or 0),
            "announcement": live.get("announcement") or "",
            "orientation": live.get("pictureOrientation") or 0,
            "is_oshi": is_oshi,
            "member": {
                "user_id": str(user.get("userId") or ""),
                "nickname": nickname,
                "star_name": star_name,
                "avatar_url": _media_url(user.get("avatar")),
                "group": _group_of(nickname, star_name),
                "level": user.get("level") or 0,
                "vip": bool(user.get("vip")),
            },
        })

    items.sort(key=lambda item: (0 if item["is_oshi"] else 1, -item["online_num"]))
    result = {
        "ok": True,
        "updated_at": int(now),
        "count": len(items),
        "items": items,
    }
    with _cache_lock:
        _cache["data"] = result
        _cache["ts"] = now
    return result


def stream_dir(live_id):
    return os.path.join(STREAM_ROOT, live_id)


def _alive(proc):
    return proc is not None and proc.poll() is None


def _log_tail(directory, limit=800):
    path = os.path.join(directory, "ffmpeg.log")
    try:
        with open(path, "rb") as fh:
            return fh.read()[-limit:].decode("utf-8", "ignore")
    except OSError:
        return ""


def _start_stream(live_id):
    info = _post(ONE_URL, {"liveId": live_id})
    content = info.get("content") or {}
    stream = content.get("playStreamPath") or ""
    if not stream:
        raise RuntimeError("直播间未开播或已结束")

    directory = stream_dir(live_id)
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory, exist_ok=True)
    playlist = os.path.join(directory, "index.m3u8")
    log_path = os.path.join(directory, "ffmpeg.log")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-i", stream,
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments",
        "-hls_segment_filename", os.path.join(directory, "seg%05d.ts"),
        playlist,
    ]
    log = open(log_path, "wb")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
    log.close()
    return {
        "proc": proc,
        "dir": directory,
        "last_access": time.time(),
        "title": content.get("title") or "",
    }


def _wait_playlist(entry, timeout=15):
    playlist = os.path.join(entry["dir"], "index.m3u8")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(playlist) and os.path.getsize(playlist) > 0:
            return playlist
        if not _alive(entry["proc"]):
            raise RuntimeError("转流失败: " + (_log_tail(entry["dir"]) or "ffmpeg 已退出"))
        time.sleep(0.3)
    raise RuntimeError("转流启动超时，请稍后重试")


def ensure_stream(live_id):
    with _streams_lock:
        entry = _streams.get(live_id)
        if entry and _alive(entry["proc"]):
            entry["last_access"] = time.time()
        else:
            entry = _start_stream(live_id)
            _streams[live_id] = entry
    return _wait_playlist(entry)


def touch(live_id):
    with _streams_lock:
        entry = _streams.get(live_id)
        if entry:
            entry["last_access"] = time.time()


def _cleanup_loop():
    while True:
        time.sleep(20)
        now = time.time()
        with _streams_lock:
            for live_id, entry in list(_streams.items()):
                if now - entry["last_access"] <= IDLE_SECONDS:
                    continue
                proc = entry["proc"]
                if _alive(proc):
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except Exception:
                        proc.kill()
                shutil.rmtree(entry["dir"], ignore_errors=True)
                del _streams[live_id]


threading.Thread(target=_cleanup_loop, daemon=True).start()
