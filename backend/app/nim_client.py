"""网易云信聊天室 TCP 客户端（只收直播弹幕）。

参考实现：Desktop48.app 的 nim-common.ts / nim-chatroom.ts。
LBS 取地址 → RSA 握手交换 RC4 密钥 → 安卓客户端身份登录聊天室 → 心跳 + 消息。
"""

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import zlib

import requests

RSA_PUBLIC_KEY_DER = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCBxLuL8+xpQSddSnSvPkvNOHdcr5Euqw+kkOSz"
    "O/buDMheCfFILRC/v5+nv8BsL7/YZWVpDA8sIBTxfNRqSCu0uLjlbJqT/sMnPT1xxdQrkb1HSnuS"
    "yTbZbqaInQ13tBE2SfcAhsQZJJ1hKQSE2QyKOMxQPhP583qcsIhDbdExvwIDAQAB"
)
RSA_KEY_VERSION = 0

LBS_URL = "https://lbs.netease.im/lbs/chat.jsp"
SERVICE_HANDSHAKE = 1
CMD_HANDSHAKE = 1
CMD_HEARTBEAT = 2
SERVICE_CHATROOM = 13
CMD_CHATROOM_LOGIN = 2
CMD_CHATROOM_MESSAGE = 7
CMD_CHATROOM_ACK = 35

HEARTBEAT_SECONDS = 15


def encode_varint(value: int) -> bytes:
    rest = int(value) & 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = rest & 0x7F
        rest //= 128
        if rest:
            byte |= 0x80
        out.append(byte)
        if not rest:
            return bytes(out)


def read_varint(buffer: bytes, offset: int = 0):
    value = 0
    scale = 1
    for index in range(offset, min(len(buffer), offset + 5)):
        byte = buffer[index]
        value += (byte & 0x7F) * scale
        if not (byte & 0x80):
            return value, index - offset + 1
        scale *= 128
    return None


def encode_bytes(value) -> bytes:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return encode_varint(len(raw)) + raw


def encode_properties(properties: dict) -> bytes:
    items = [(int(key), value) for key, value in properties.items()
             if value is not None and value != ""]
    items.sort(key=lambda pair: pair[0])
    out = bytearray(encode_varint(len(items)))
    for key, value in items:
        out += encode_varint(key)
        out += encode_bytes(value)
    return bytes(out)


def decode_properties(body: bytes) -> dict:
    count = read_varint(body, 0)
    if not count:
        raise ValueError("属性表不完整")
    total, size = count
    offset = size
    result = {}
    for _ in range(total):
        key = read_varint(body, offset)
        if not key:
            raise ValueError("属性表 key 不完整")
        key_value, key_size = key
        length = read_varint(body, offset + key_size)
        if not length:
            raise ValueError("属性表长度不完整")
        length_value, length_size = length
        offset += key_size + length_size
        if offset + length_value > len(body):
            raise ValueError("属性表内容越界")
        result[key_value] = body[offset:offset + length_value]
        offset += length_value
    return result


def prop_text(properties: dict, key: int) -> str:
    value = properties.get(key)
    return value.decode("utf-8", "ignore").strip() if value else ""


def make_packet(service: int, command: int, serial: int, body: bytes = b"",
                tag: int = 0) -> bytes:
    header = bytes([service, command]) + struct.pack("<h", serial) + bytes([tag])
    return encode_varint(len(header) + len(body)) + header + body


def decode_packet(raw: bytes, embedded: bool = False) -> dict:
    length = read_varint(raw, 0)
    if not length:
        raise ValueError("数据帧不完整")
    frame_length, size = length
    if len(raw) < size + 5:
        raise ValueError("数据帧不完整")
    offset = size
    service = raw[offset]
    command = raw[offset + 1]
    serial = struct.unpack_from("<h", raw, offset + 2)[0]
    tag = raw[offset + 4]
    offset += 5
    result = 200
    if tag & 2:
        if offset + 2 > len(raw):
            raise ValueError("响应状态不完整")
        result = struct.unpack_from("<h", raw, offset)[0]
        offset += 2
    end = len(raw) if embedded else min(len(raw), size + frame_length)
    body = raw[offset:end]
    if tag & 1:
        if len(body) < 4:
            raise ValueError("压缩帧不完整")
        expected = struct.unpack_from("<i", body, 0)[0]
        body = zlib.decompress(body[4:])
        if len(body) != expected:
            raise ValueError("压缩帧长度不一致")
    return {"service": service, "command": command, "serial": serial,
            "result": result, "body": body}


def decode_embedded(raw: bytes):
    length = read_varint(raw, 0)
    if not length:
        return None
    frame_length, size = length
    if len(raw) < size + 5:
        return None
    return decode_packet(raw, embedded=True)


class Rc4:
    def __init__(self, key: bytes):
        if not key:
            raise ValueError("加密密钥为空")
        self.state = list(range(256))
        cursor = 0
        for index in range(256):
            cursor = (cursor + self.state[index] + key[index % len(key)]) & 0xFF
            self.state[index], self.state[cursor] = self.state[cursor], self.state[index]
        self.x = 0
        self.y = 0

    def apply(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        for index, byte in enumerate(data):
            self.x = (self.x + 1) & 0xFF
            self.y = (self.y + self.state[self.x]) & 0xFF
            self.state[self.x], self.state[self.y] = (
                self.state[self.y], self.state[self.x])
            out[index] = byte ^ self.state[
                (self.state[self.x] + self.state[self.y]) & 0xFF]
        return bytes(out)


_PEM_LINES = [RSA_PUBLIC_KEY_DER[i:i + 64]
              for i in range(0, len(RSA_PUBLIC_KEY_DER), 64)]
_PEM = ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(_PEM_LINES)
        + "\n-----END PUBLIC KEY-----\n")


def rsa_encrypt(payload: bytes) -> bytes:
    pem_fd, pem_path = tempfile.mkstemp(suffix=".pem")
    in_fd, in_path = tempfile.mkstemp(suffix=".bin")
    try:
        with os.fdopen(pem_fd, "w") as fh:
            fh.write(_PEM)
        with os.fdopen(in_fd, "wb") as fh:
            fh.write(payload)
        return subprocess.check_output([
            "openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", pem_path,
            "-pkeyopt", "rsa_padding_mode:pkcs1", "-in", in_path,
        ])
    finally:
        for path in (pem_path, in_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def make_handshake(key: bytes, login_packet: bytes) -> bytes:
    payload = encode_bytes(key) + login_packet
    blocks = [rsa_encrypt(payload[offset:offset + 117])
              for offset in range(0, len(payload), 117)]
    return make_packet(SERVICE_HANDSHAKE, CMD_HANDSHAKE, 0,
                       struct.pack("<i", RSA_KEY_VERSION) + b"".join(blocks))


def build_login_body(app_key: str, account: str, token: str, room_id: str,
                     device_id: str, sdk_version: int, package_name: str) -> bytes:
    room_properties = encode_properties({
        1: app_key, 2: account, 3: device_id, 5: room_id, 8: 1,
    })
    auth_properties = encode_properties({
        3: 1, 4: "8.0.0", 6: sdk_version, 9: 1, 13: device_id,
        18: app_key, 19: account, 25: package_name, 1000: token,
    })
    return b"\x02" + room_properties + auth_properties


class NimChatroomClient:
    def __init__(self, app_key: str, account: str, token: str, room_id: str,
                 device_id: str, sdk_version: int = 92012,
                 package_name: str = "com.pocket.snh48.activity",
                 on_message=None, on_status=None):
        self.app_key = app_key
        self.account = account
        self.token = token
        self.room_id = str(room_id)
        self.device_id = device_id
        self.sdk_version = sdk_version
        self.package_name = package_name
        self.on_message = on_message
        self.on_status = on_status or (lambda status, detail="": None)
        self.sock = None
        self.encryptor = None
        self.decryptor = None
        self.buffer = b""
        self.serial = 1
        self.connected = False
        self.stopped = False
        self._heartbeat = None
        self._reader = None
        self._seen = set()

    def _next_serial(self) -> int:
        value = self.serial
        self.serial = 1 if value >= 32767 else value + 1
        return value

    def _address(self):
        params = {
            "k": self.app_key, "id": self.account, "rid": self.room_id,
            "v": str(self.sdk_version), "tp": "1", "dt": "0", "nt": "2",
        }
        resp = requests.get(LBS_URL, params=params, timeout=15)
        resp.raise_for_status()
        link = (resp.json() or {}).get("link")
        address = str(link[0] if isinstance(link, list) and link else "").strip()
        host, _, port = address.rpartition(":")
        host = host.strip("[]")
        if not host or not port.isdigit():
            raise RuntimeError("云信未返回有效的聊天室地址")
        return host, int(port)

    def _login_packet(self) -> bytes:
        body = build_login_body(self.app_key, self.account, self.token,
                                self.room_id, self.device_id,
                                self.sdk_version, self.package_name)
        return make_packet(SERVICE_CHATROOM, CMD_CHATROOM_LOGIN,
                           self._next_serial(), body)

    def _frames(self):
        while True:
            length = read_varint(self.buffer, 0)
            if not length:
                return
            frame_length, size = length
            total = size + frame_length
            if len(self.buffer) < total:
                return
            raw = self.buffer[:total]
            self.buffer = self.buffer[total:]
            packet = decode_packet(raw)
            if (packet["service"] == 4 and packet["command"] in (1, 2, 10, 11)
                    and len(packet["body"]) > 8):
                embedded = decode_embedded(packet["body"][8:])
                if embedded:
                    packet = embedded
            yield packet

    def connect(self, timeout: int = 20) -> bool:
        host, port = self._address()
        key = os.urandom(16)
        login_packet = self._login_packet()
        handshake = make_handshake(key, login_packet)
        self.encryptor = Rc4(key)
        self.decryptor = Rc4(key)
        self.buffer = b""

        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(handshake)
        enter_sent = False
        deadline = time.time() + timeout
        logged_in = False
        try:
            while time.time() < deadline and not logged_in:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                self.buffer += self.decryptor.apply(chunk)
                for packet in self._frames():
                    if packet["service"] == SERVICE_HANDSHAKE and packet["command"] == CMD_HANDSHAKE:
                        if packet["result"] != 200:
                            raise RuntimeError(f"聊天室握手失败({packet['result']})")
                        if not enter_sent:
                            enter_sent = True
                            sock.sendall(self.encryptor.apply(login_packet))
                    elif packet["service"] == SERVICE_CHATROOM and packet["command"] == CMD_CHATROOM_LOGIN:
                        if packet["result"] != 200:
                            raise RuntimeError(f"进入聊天室被拒({packet['result']})")
                        logged_in = True
                    elif packet["service"] == SERVICE_CHATROOM and packet["command"] == CMD_CHATROOM_MESSAGE:
                        self._handle_message(packet["body"])
        except Exception:
            sock.close()
            raise
        if not logged_in:
            sock.close()
            raise RuntimeError("进入聊天室超时")

        sock.settimeout(None)
        self.sock = sock
        self.connected = True
        self.on_status("connected")
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat.start()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return True

    def _read_loop(self):
        try:
            while not self.stopped and self.sock:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.buffer += self.decryptor.apply(chunk)
                for packet in self._frames():
                    if packet["service"] == SERVICE_CHATROOM and packet["command"] == CMD_CHATROOM_MESSAGE:
                        self._handle_message(packet["body"])
        except Exception:
            pass
        finally:
            self.connected = False
            if not self.stopped:
                self.on_status("disconnected")

    def _heartbeat_loop(self):
        while not self.stopped and self.connected and self.sock:
            time.sleep(HEARTBEAT_SECONDS)
            if self.stopped or not self.sock:
                return
            try:
                packet = make_packet(SERVICE_HANDSHAKE, CMD_HEARTBEAT,
                                     self._next_serial())
                self.sock.sendall(self.encryptor.apply(packet))
            except Exception:
                return

    def _handle_message(self, body: bytes):
        try:
            props = decode_properties(body)
        except Exception:
            return
        uuid = prop_text(props, 1)
        msg_type = int(prop_text(props, 2) or -1)
        attach = prop_text(props, 3)
        custom = prop_text(props, 4)
        message = {
            "id": uuid,
            "type": "text" if msg_type == 0 else "custom",
            "msg_type": msg_type,
            "text": attach if msg_type == 0 else prop_text(props, 13),
            "custom": custom,
            "nick": prop_text(props, 7),
            "avatar": prop_text(props, 8),
            "from": prop_text(props, 21),
            "time": int(prop_text(props, 20) or time.time() * 1000),
        }
        if custom:
            try:
                ext = json.loads(custom)
                user = ext.get("user") or {}
                message["nick"] = message["nick"] or user.get("nickName", "")
                message["avatar"] = message["avatar"] or user.get("avatar", "")
                if not message["text"]:
                    message["text"] = ext.get("text") or ext.get("content") or ""
            except Exception:
                pass
        if uuid:
            if uuid in self._seen:
                return
            self._seen.add(uuid)
        try:
            self.on_message(message)
        except Exception:
            pass
        if prop_text(props, 38) == "1" and uuid and self.sock:
            try:
                ack = make_packet(SERVICE_CHATROOM, CMD_CHATROOM_ACK,
                                  self._next_serial(),
                                  encode_properties({1: uuid, 2: self.room_id}))
                self.sock.sendall(self.encryptor.apply(ack))
            except Exception:
                pass

    def close(self):
        self.stopped = True
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
