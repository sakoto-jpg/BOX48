"""口袋48 房间留言：登录 token + HTTP 轮询。

接口（均需登录 token）：
- im/api/v1/im/server/jump            {starId, targetType:1} -> serverId/channelId
- im/api/v1/team/message/list/all     {channelId, serverId, nextTime, limit}
- im/api/v1/team/message/list/homeowner  同上，只看房主

token 存放在 /config/pocket48.yaml（config/ 已被 .gitignore 忽略）。
"""

import os

import requests
import yaml

BASE = "https://pocketapi.48.cn"
CONFIG_PATH = os.environ.get("POCKET48_CONFIG_PATH", "/config/pocket48.yaml")

APP_INFO = (
    '{"vendor":"apple","deviceId":"0","appVersion":"6.2.0","appBuild":"200701",'
    '"osVersion":"14.4.0","osType":"ios","deviceName":"ipad","os":"ios"}'
)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def save_config(data: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)


def _headers(token: str, pa: str = "") -> dict:
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": "PocketFans201807/6.2.0 (iPad; iOS 14.4; Scale/2.00)",
        "appInfo": APP_INFO,
        "token": token,
    }
    if pa:
        headers["pa"] = pa
    return headers


def _post(path: str, payload: dict, token: str, pa: str = "") -> dict:
    resp = requests.post(BASE + path, json=payload,
                         headers=_headers(token, pa), timeout=20)
    resp.raise_for_status()
    return resp.json()


def resolve_channel(token: str, user_id: str, pa: str = ""):
    data = _post("/im/api/v1/im/server/jump",
                 {"starId": str(user_id), "targetType": 1}, token, pa)
    if data.get("status") != 200:
        raise RuntimeError(data.get("message") or f"解析房间失败({data.get('status')})")
    content = data.get("content") or {}
    server_id = content.get("serverId")
    channel_id = content.get("channelId")
    if not server_id or not channel_id:
        raise RuntimeError("该成员没有可用房间")
    return server_id, channel_id


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _normalize(msg: dict) -> dict:
    user = msg.get("user") or msg.get("userInfo") or {}
    body = msg.get("bodys") or msg.get("body") or []
    text = ""
    if isinstance(body, list):
        parts = []
        for part in body:
            if isinstance(part, dict):
                parts.append(str(_first(part.get("msg"), part.get("text"), "") or ""))
            elif isinstance(part, str):
                parts.append(part)
        text = "".join(parts)
    if not text:
        text = str(_first(msg.get("content"), msg.get("text"), "") or "")
    return {
        "id": str(_first(msg.get("msgIdClient"), msg.get("msgidClient"),
                         msg.get("msgId"), "") or ""),
        "time": int(_first(msg.get("msgTime"), msg.get("time"),
                           msg.get("createTime"), 0) or 0),
        "type": _first(msg.get("msgType"), msg.get("type"), 0),
        "text": text,
        "user": {
            "id": str(_first(user.get("userId"), user.get("id"), user.get("uid"), "") or ""),
            "name": str(_first(user.get("nickName"), user.get("nickname"),
                               user.get("name"), "") or ""),
            "avatar": str(_first(user.get("avatar"), user.get("userAvatar"), "") or ""),
            "level": user.get("level") or 0,
        },
    }


def list_messages(token: str, server_id, channel_id, scope: str = "all",
                  next_time=0, limit: int = 20, pa: str = "") -> dict:
    kind = "homeowner" if scope == "homeowner" else "all"
    data = _post(f"/im/api/v1/team/message/list/{kind}", {
        "channelId": channel_id,
        "serverId": server_id,
        "nextTime": str(next_time),
        "limit": int(limit),
    }, token, pa)
    if data.get("status") != 200:
        raise RuntimeError(data.get("message") or f"获取留言失败({data.get('status')})")
    content = data.get("content") or {}
    raw = content.get("message") or content.get("messageList") or []
    return {
        "items": [_normalize(item) for item in raw if isinstance(item, dict)],
        "next_time": content.get("nextTime") or 0,
    }


def fetch_room_messages(user_id: str, scope: str = "all",
                        next_time=0, limit: int = 20) -> dict:
    config = load_config()
    token = str(config.get("token") or "").strip()
    pa = str(config.get("pa") or "").strip()
    if not token:
        raise RuntimeError("尚未配置口袋48 token")
    server_id, channel_id = resolve_channel(token, user_id, pa)
    return list_messages(token, server_id, channel_id,
                         scope=scope, next_time=next_time, limit=limit, pa=pa)
