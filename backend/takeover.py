"""首次接管：解析外部现有 config.json，把支持的入站导入面板 db。

仅导入 type 在 shadowsocks/vmess/vless/trojan/hysteria2 中的入站；
tag/port/凭证照搬；解析失败的条目跳过并记录原因。
"""
import json
import os
import time

from config_mgr import backup_timestamped, config_path
from db import _conn, get_setting, new_id, set_setting
from singbox import derive_reality_public

SUPPORTED = ("shadowsocks", "vmess", "vless", "trojan", "hysteria2")


def _parse_inbound(ib: dict) -> dict:
    """sing-box inbound JSON -> 面板 db 行字段；不支持则返回 (None, 原因)"""
    proto = ib.get("type", "")
    if proto not in SUPPORTED:
        return None, f"不支持的协议类型: {proto or '未知'}"
    port = ib.get("listen_port")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return None, "端口缺失或非法"
    tag = ib.get("tag") or f"{proto}-{port}"
    settings = {}

    try:
        if proto == "shadowsocks":
            settings["method"] = ib.get("method", "aes-256-gcm")
            settings["credential"] = ib.get("password", "")
            settings["backups"] = [u.get("password", "") for u in ib.get("users", [])
                                   if u.get("password")]
        elif proto == "vmess":
            users = ib.get("users", [])
            if not users or not users[0].get("uuid"):
                return None, "vmess 缺少 users[0].uuid"
            settings["credential"] = users[0]["uuid"]
            settings["backups"] = [u["uuid"] for u in users[1:] if u.get("uuid")][:2]
        elif proto == "vless":
            users = ib.get("users", [])
            if not users or not users[0].get("uuid"):
                return None, "vless 缺少 users[0].uuid"
            settings["credential"] = users[0]["uuid"]
            settings["backups"] = [u["uuid"] for u in users[1:] if u.get("uuid")][:2]
            settings["flow"] = users[0].get("flow", "xtls-rprx-vision")
            tls = ib.get("tls") or {}
            reality = tls.get("reality") or {}
            if reality.get("enabled"):
                priv = reality.get("private_key", "")
                pub, no_priv = "", False
                if priv:
                    try:
                        pub = derive_reality_public(priv)
                    except Exception:
                        no_priv = True
                else:
                    no_priv = True
                settings["reality"] = {
                    "server_name": tls.get("server_name", ""),
                    "private_key": priv,
                    "public_key": pub,
                    "short_id": (reality.get("short_id") or [""])[0],
                }
                if no_priv:
                    # 私钥缺失则无法还原：写配置时会明确报错，提示用户重建该入站
                    settings["reality"]["_no_private_key"] = True
        elif proto == "trojan":
            users = ib.get("users", [])
            if not users or not users[0].get("password"):
                return None, "trojan 缺少 users[0].password"
            settings["credential"] = users[0]["password"]
            settings["backups"] = [u["password"] for u in users[1:] if u.get("password")][:2]
            tls = ib.get("tls") or {}
            reality = tls.get("reality") or {}
            if reality.get("enabled"):
                priv = reality.get("private_key", "")
                pub, no_priv = "", False
                if priv:
                    try:
                        pub = derive_reality_public(priv)
                    except Exception:
                        no_priv = True
                else:
                    no_priv = True
                settings["reality"] = {
                    "server_name": tls.get("server_name", ""),
                    "private_key": priv,
                    "public_key": pub,
                    "short_id": (reality.get("short_id") or [""])[0],
                }
                if no_priv:
                    # 私钥缺失则无法还原：写配置时会明确报错，提示用户重建该入站
                    settings["reality"]["_no_private_key"] = True
        elif proto == "hysteria2":
            users = ib.get("users", [])
            if not users or not users[0].get("password"):
                return None, "hysteria2 缺少 users[0].password"
            settings["credential"] = users[0]["password"]
            settings["backups"] = [u["password"] for u in users[1:] if u.get("password")][:2]
            tls = ib.get("tls") or {}
            settings["cert_path"] = tls.get("certificate_path", "")
            settings["key_path"] = tls.get("key_path", "")
    except Exception as e:
        return None, f"解析异常: {e}"

    if not settings.get("credential"):
        return None, "主凭证为空"
    return {"tag": tag, "protocol": proto, "port": port, "settings": settings}, ""


def import_existing() -> dict:
    """执行接管导入；返回 {backup, imported, skipped}"""
    p = config_path()
    if not os.path.isfile(p):
        raise ValueError(f"配置文件不存在: {p}")

    backup = backup_timestamped()

    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    inbounds = cfg.get("inbounds", []) or []

    c = _conn()
    existing_ports = {r["port"] for r in c.execute("SELECT port FROM inbounds").fetchall()}

    imported, skipped = [], []
    for ib in inbounds:
        parsed, reason = _parse_inbound(ib if isinstance(ib, dict) else {})
        tag = (ib.get("tag") if isinstance(ib, dict) else "") or "?"
        if not parsed:
            skipped.append({"tag": tag, "reason": reason})
            continue
        if parsed["port"] in existing_ports:
            skipped.append({"tag": parsed["tag"], "reason": f"端口 {parsed['port']} 已在面板中"})
            continue
        ib_id = new_id()
        c.execute(
            "INSERT INTO inbounds(id, tag, protocol, port, enabled, settings, created_at)"
            " VALUES(?,?,?,?,1,?,?)",
            (ib_id, parsed["tag"], parsed["protocol"], parsed["port"],
             json.dumps(parsed["settings"]), time.time()),
        )
        c.execute("INSERT OR IGNORE INTO inbound_traffic(inbound_id, updated_at) VALUES(?, ?)",
                  (ib_id, time.time()))
        existing_ports.add(parsed["port"])
        imported.append(parsed["tag"])

    c.commit()
    c.close()
    set_setting("takeover_done", "1")
    return {"backup": backup, "imported": imported, "skipped": skipped}
