"""口袋48 房间留言：登录 token + HTTP 轮询。

接口（均需登录 token）：
- im/api/v1/im/server/jump            {starId, targetType:1} -> serverId/channelId
- im/api/v1/team/message/list/all     {channelId, serverId, nextTime, limit}
- im/api/v1/team/message/list/homeowner  同上，只看房主

token 存放在 /config/pocket48.yaml（config/ 已被 .gitignore 忽略）。
"""

import json
import os
import re
import secrets

import requests
import yaml

BASE = "https://pocketapi.48.cn"
CONFIG_PATH = os.environ.get("POCKET48_CONFIG_PATH", "/config/pocket48.yaml")

# 2021 年签名，参考实现实测服务端不拦截；无需 WASM 动态生成
POCKET_PA = (
    "MTYzNjAxNzMxNTAwMCw3NjkyNzVkNTliZWU0MmQ0OGVjOWIyYTMwY2FkOWQzOSw3NzFhY2QwY2Ji"
    "ZDhkZDUzZDdjZGY3YTcxNGYzNmVlOCwyMDIxMDYwOTAx"
)

ANDROID_DEVICE = {
    "model": "MI 8",
    "osVersion": "10",
    "brand": "Xiaomi",
    "buildId": "QP1A.190711.020",
    "buildTags": "release-keys",
}


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


def _device_id(config: dict = None) -> str:
    config = config if config is not None else load_config()
    device_id = str(config.get("device_id") or "")
    if not re.fullmatch(r"[0-9a-f]{16}", device_id):
        device_id = secrets.token_hex(8)
        config["device_id"] = device_id
        save_config(config)
    return device_id


def _headers(token: str = None, pa: str = "", device_id: str = None) -> dict:
    device_id = device_id or _device_id()
    device = ANDROID_DEVICE
    user_agent = (
        f"PocketFans201807/7.1.44_26092001 ({device['model']}:Android "
        f"{device['osVersion']};{device['brand']} {device['buildId']} {device['buildTags']})"
    )
    app_info = json.dumps({
        "IMEI": device_id,
        "appBuild": "26092001",
        "appName": "pocket48",
        "appVersion": "7.1.44",
        "deviceId": device_id,
        "deviceName": device["model"],
        "osType": "android",
        "osVersion": device["osVersion"],
        "phoneName": device["model"],
        "phoneSystemVersion": device["osVersion"],
        "vendor": device["brand"],
    }, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": user_agent,
        "appInfo": app_info,
        "Accept-Language": "zh-Hans-CN;q=1",
        "pa": pa or POCKET_PA,
    }
    if token:
        headers["token"] = token
    return headers


def _post(path: str, payload: dict, token: str = None, pa: str = "") -> dict:
    resp = requests.post(BASE + path, json=payload,
                         headers=_headers(token, pa), timeout=20)
    resp.raise_for_status()
    return resp.json()


def send_sms(mobile: str, area: str = "86") -> dict:
    mobile = str(mobile or "").strip()
    if not re.fullmatch(r"\d{6,15}", mobile):
        raise RuntimeError("手机号格式不正确")
    data = _post("/user/api/v2/sms/send_sms", {
        "mobile": mobile,
        "area": str(area or "86"),
        "businessCode": 1,
        "intelligenceToken": "",
    })
    if data.get("status") == 2001:
        raise RuntimeError(data.get("message") or "需要完成人机验证，请在官方 APP 内登录")
    if not data.get("success"):
        raise RuntimeError(data.get("message") or "发送验证码失败")
    return {"ok": True, "message": "验证码已发送"}


def login_with_sms(mobile: str, code: str, area: str = "86") -> dict:
    mobile = str(mobile or "").strip()
    code = str(code or "").strip()
    if not mobile or not code:
        raise RuntimeError("手机号与验证码不能为空")
    config = load_config()
    device_id = _device_id(config)
    resp = requests.post(BASE + "/user/api/v2/login/app/app_login", json={
        "mobileCodeLogin": {"mobile": mobile, "code": code,
                            "area": str(area or "86")},
        "loginType": "MOBILE_SMS_CODE",
        "deviceToken": "",
    }, headers=_headers(device_id=device_id), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    token = (data.get("content") or {}).get("token")
    if not token:
        raise RuntimeError(data.get("message") or "登录失败")
    config["token"] = token
    config["mobile"] = mobile
    config["area"] = str(area or "86")
    save_config(config)
    return {"ok": True, "message": "登录成功", "configured": True}


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


def _media(path):
    if not path:
        return ""
    text = str(path)
    return text if text.startswith("http") else "https://source.48.cn" + text


def _normalize(msg: dict) -> dict:
    body = msg.get("bodys")
    text = ""
    if isinstance(body, str):
        text = body
        if text.lstrip().startswith("{"):
            try:
                parsed = json.loads(text)
                text = str(_first(parsed.get("replyText"), parsed.get("text"),
                                  parsed.get("content"), "") or text)
            except Exception:
                pass
    elif isinstance(body, list):
        parts = []
        for part in body:
            if isinstance(part, dict):
                parts.append(str(_first(part.get("msg"), part.get("text"), "") or ""))
            elif isinstance(part, str):
                parts.append(part)
        text = "".join(parts)
    elif isinstance(body, dict):
        text = str(_first(body.get("msg"), body.get("text"), "") or "")
    if not text:
        text = str(_first(msg.get("content"), msg.get("text"), "") or "")

    ext = msg.get("extInfo")
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except Exception:
            ext = {}
    if not isinstance(ext, dict):
        ext = {}
    user = ext.get("user") or msg.get("user") or msg.get("userInfo") or {}

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
            "avatar": _media(_first(user.get("avatar"), user.get("userAvatar"), "")),
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
