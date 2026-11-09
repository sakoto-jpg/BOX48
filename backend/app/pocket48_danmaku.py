"""口袋48 直播弹幕会话：按 liveId 维护 NIM 聊天室连接与消息缓冲。"""

import threading
import time

import requests

import nim_client
import pocket48_room

APP_KEY = "632feff1f4c838541ab75195d1ceb3fa"
SDK_VERSION = 92012
PACKAGE_NAME = "com.pocket.snh48.activity"
IDLE_SECONDS = 120
MAX_MESSAGES = 300

_sessions = {}
_lock = threading.Lock()


def _room_id(live_id: str, token: str) -> str:
    data = pocket48_room._post("/live/api/v1/live/getLiveOne",
                               {"liveId": live_id}, token)
    content = data.get("content") or {}
    room_id = content.get("roomId")
    if not room_id:
        raise RuntimeError("直播间未开播或缺少聊天室 ID")
    return str(room_id)


def _credentials(token: str):
    headers = pocket48_room._headers(token)
    headers["P-Sign-Type"] = "V0"
    resp = requests.post("https://pocketapi.48.cn/im/api/v1/im/userinfo",
                         json={}, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    content = data.get("content") or {}
    accid = content.get("accid") or content.get("accId") or content.get("imUserId")
    password = content.get("pwd") or content.get("token") or content.get("imPwd")
    if not accid or not password:
        raise RuntimeError(data.get("message") or "获取 IM 凭据失败")
    return str(accid), str(password)


def ensure_session(live_id: str):
    with _lock:
        session = _sessions.get(live_id)
        if session and session["client"].connected:
            session["last_access"] = time.time()
            return session

    config = pocket48_room.load_config()
    token = str(config.get("token") or "").strip()
    if not token:
        raise RuntimeError("尚未配置口袋48 token")
    room_id = _room_id(live_id, token)
    accid, password = _credentials(token)
    device_id = pocket48_room._device_id(config)

    session = {
        "messages": [],
        "last_access": time.time(),
        "seq": 0,
        "status": "connecting",
        "room_id": room_id,
    }

    def on_message(message):
        session["seq"] += 1
        message["seq"] = session["seq"]
        session["messages"].append(message)
        del session["messages"][:-MAX_MESSAGES]
        session["last_access"] = time.time()

    def on_status(status, detail=""):
        session["status"] = status

    client = nim_client.NimChatroomClient(
        APP_KEY, accid, password, room_id, device_id,
        sdk_version=SDK_VERSION, package_name=PACKAGE_NAME,
        on_message=on_message, on_status=on_status,
    )
    session["client"] = client
    with _lock:
        _sessions[live_id] = session
    try:
        client.connect()
    except Exception:
        with _lock:
            if _sessions.get(live_id) is session:
                _sessions.pop(live_id, None)
        raise
    return session


def poll(live_id: str, since: int = 0) -> dict:
    session = ensure_session(live_id)
    session["last_access"] = time.time()
    messages = [item for item in session["messages"] if item["seq"] > int(since)]
    return {
        "status": session["status"],
        "seq": session["seq"],
        "room_id": session["room_id"],
        "items": messages[-100:],
    }


def stop(live_id: str) -> None:
    with _lock:
        session = _sessions.pop(live_id, None)
    if session:
        session["client"].close()


def _cleanup_loop():
    while True:
        time.sleep(30)
        now = time.time()
        with _lock:
            stale = [live_id for live_id, session in _sessions.items()
                     if now - session["last_access"] > IDLE_SECONDS]
        for live_id in stale:
            stop(live_id)


threading.Thread(target=_cleanup_loop, daemon=True).start()
