"""入站管理 CRUD（单用户轻量版）
每个入站自带一个主凭证（自动生成），最多再加 2 个备用凭证。
凭证存入 settings JSON，不再使用独立 users 表。
"""
import base64
import json
import os
import secrets
import socket
import subprocess
import time
import uuid as _uuid

from db import DATA_DIR, _conn, new_id, row_to_dict
from singbox import generate_reality_keypair

PROTOCOLS = ["shadowsocks", "vmess", "vless", "trojan", "hysteria2"]
SS_METHODS = ["aes-256-gcm", "aes-128-gcm", "chacha20-ietf-poly1305",
              "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm"]
MAX_BACKUPS = 2


def _is_uuid_proto(proto: str) -> bool:
    return proto in ("vmess", "vless")


def gen_credential(protocol: str, method: str = "") -> str:
    """按协议生成合法凭证（UUID 用 Python 生成，不依赖 sing-box 二进制）"""
    if _is_uuid_proto(protocol):
        return str(_uuid.uuid4())
    if protocol == "shadowsocks" and method == "2022-blake3-aes-256-gcm":
        return base64.b64encode(secrets.token_bytes(32)).decode()
    if protocol == "shadowsocks" and method == "2022-blake3-aes-128-gcm":
        return base64.b64encode(secrets.token_bytes(16)).decode()
    return secrets.token_urlsafe(16)


def port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def port_used_by_inbound(port: int, exclude_id: str = "") -> bool:
    c = _conn()
    r = c.execute("SELECT id FROM inbounds WHERE port=? AND id!=?", (port, exclude_id)).fetchone()
    c.close()
    return r is not None


def _gen_hy2_cert(tag: str) -> tuple:
    d = os.path.join(DATA_DIR, "certs")
    os.makedirs(d, exist_ok=True)
    cert = os.path.join(d, f"{tag}.crt")
    key = os.path.join(d, f"{tag}.key")
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key, "-out", cert, "-days", "3650", "-nodes",
            "-subj", "/CN=localhost",
        ], capture_output=True, timeout=30, check=True)
    return cert, key


def create_inbound(protocol: str, port: int, tag: str = "", settings: dict = None) -> dict:
    if protocol not in PROTOCOLS:
        raise ValueError(f"不支持的协议: {protocol}")
    if not (1 <= port <= 65535):
        raise ValueError("端口范围 1-65535")
    if port_used_by_inbound(port):
        raise ValueError(f"端口 {port} 已被其他入站使用")
    if port_in_use(port):
        raise ValueError(f"端口 {port} 已被系统其他进程占用")

    settings = dict(settings or {})
    ib_id = new_id()
    tag = tag or f"{protocol}-{port}"
    method = settings.get("method", "aes-256-gcm")

    # 主凭证自动生成（允许手动指定）
    if not settings.get("credential"):
        settings["credential"] = gen_credential(protocol, method)
    settings["backups"] = []

    if protocol == "shadowsocks":
        if method not in SS_METHODS:
            raise ValueError(f"不支持的加密方式: {method}")
        settings["method"] = method
    elif protocol in ("vless", "trojan"):
        if settings.get("reality", True):
            kp = generate_reality_keypair()
            settings["flow"] = "xtls-rprx-vision"
            settings["reality"] = {
                "server_name": settings.get("server_name", "www.microsoft.com"),
                "private_key": kp["private_key"],
                "public_key": kp["public_key"],
                "short_id": secrets.token_hex(4),
            }
    elif protocol == "hysteria2":
        cert, key = _gen_hy2_cert(tag)
        settings["cert_path"] = cert
        settings["key_path"] = key

    # 清理不需要持久化的临时键
    settings.pop("server_name", None)

    c = _conn()
    c.execute(
        "INSERT INTO inbounds(id, tag, protocol, port, enabled, settings, created_at) VALUES(?,?,?,?,1,?,?)",
        (ib_id, tag, protocol, port, json.dumps(settings), time.time()),
    )
    c.execute("INSERT OR IGNORE INTO inbound_traffic(inbound_id, updated_at) VALUES(?, ?)",
              (ib_id, time.time()))
    c.commit()
    c.close()
    return get_inbound(ib_id)


def get_inbound(ib_id: str) -> dict:
    c = _conn()
    r = c.execute("SELECT * FROM inbounds WHERE id=?", (ib_id,)).fetchone()
    c.close()
    d = row_to_dict(r)
    if d:
        d["settings"] = json.loads(d["settings"] or "{}")
    return d


def list_inbounds() -> list:
    c = _conn()
    rows = c.execute("SELECT * FROM inbounds ORDER BY port").fetchall()
    c.close()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["settings"] = json.loads(d["settings"] or "{}")
        out.append(d)
    return out


def update_inbound(ib_id: str, **fields) -> dict:
    ib = get_inbound(ib_id)
    if not ib:
        raise ValueError("入站不存在")
    if "port" in fields and fields["port"] != ib["port"]:
        port = fields["port"]
        if port_used_by_inbound(port, ib_id):
            raise ValueError(f"端口 {port} 已被其他入站使用")
        if port_in_use(port):
            raise ValueError(f"端口 {port} 已被系统其他进程占用")
    allowed = {"tag", "port", "enabled"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "enabled" in sets:
        sets["enabled"] = 1 if sets["enabled"] else 0
    if sets:
        c = _conn()
        c.execute(f"UPDATE inbounds SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                  (*sets.values(), ib_id))
        c.commit()
        c.close()
    return get_inbound(ib_id)


def delete_inbound(ib_id: str):
    c = _conn()
    c.execute("DELETE FROM inbounds WHERE id=?", (ib_id,))
    c.execute("DELETE FROM inbound_traffic WHERE inbound_id=?", (ib_id,))
    c.commit()
    c.close()


# ---------- 备用凭证（最多2个） ----------
def add_backup(ib_id: str, credential: str = "") -> dict:
    ib = get_inbound(ib_id)
    if not ib:
        raise ValueError("入站不存在")
    st = ib["settings"]
    backups = st.get("backups", [])
    if len(backups) >= MAX_BACKUPS:
        raise ValueError(f"备用凭证最多 {MAX_BACKUPS} 个")
    cred = credential or gen_credential(ib["protocol"], st.get("method", ""))
    if cred in backups or cred == st.get("credential"):
        raise ValueError("凭证已存在")
    backups.append(cred)
    st["backups"] = backups
    c = _conn()
    c.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(st), ib_id))
    c.commit()
    c.close()
    return get_inbound(ib_id)


def del_backup(ib_id: str, credential: str) -> dict:
    ib = get_inbound(ib_id)
    if not ib:
        raise ValueError("入站不存在")
    st = ib["settings"]
    st["backups"] = [b for b in st.get("backups", []) if b != credential]
    c = _conn()
    c.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(st), ib_id))
    c.commit()
    c.close()
    return get_inbound(ib_id)


def regen_credential(ib_id: str) -> dict:
    """重新生成主凭证"""
    ib = get_inbound(ib_id)
    if not ib:
        raise ValueError("入站不存在")
    st = ib["settings"]
    st["credential"] = gen_credential(ib["protocol"], st.get("method", ""))
    c = _conn()
    c.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(st), ib_id))
    c.commit()
    c.close()
    return get_inbound(ib_id)
