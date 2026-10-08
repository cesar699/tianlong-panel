"""config.json 生成 / 读写 / 校验（单用户轻量版）"""
import json
import os
import subprocess

from db import DATA_DIR, _conn, get_setting, row_to_dict
from singbox import bin_path, ensure_binary

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "sing-box.log")
CLASH_PORT = 19090


def _all_credentials(st: dict) -> list:
    """主凭证 + 备用凭证"""
    creds = [st["credential"]] if st.get("credential") else []
    creds += [b for b in st.get("backups", []) if b and b not in creds]
    return creds


def _inbound_config(ib: dict) -> dict:
    st = ib["settings"]
    proto = ib["protocol"]
    creds = _all_credentials(st)
    base = {"tag": ib["tag"], "listen": "0.0.0.0", "listen_port": ib["port"]}

    if proto == "shadowsocks":
        cfg = {**base, "type": "shadowsocks",
               "method": st.get("method", "aes-256-gcm"),
               "password": creds[0] if creds else ""}
        if len(creds) > 1:
            cfg["users"] = [{"name": f"backup{i}", "password": c}
                            for i, c in enumerate(creds[1:], 1)]
        return cfg

    if proto == "vmess":
        return {**base, "type": "vmess",
                "users": [{"uuid": c, "alterId": 0} for c in creds]}

    if proto == "vless":
        cfg = {**base, "type": "vless",
               "users": [{"uuid": c, "flow": st.get("flow", "xtls-rprx-vision")} for c in creds]}
        if st.get("reality"):
            r = st["reality"]
            cfg["tls"] = {
                "enabled": True,
                "server_name": r["server_name"],
                "reality": {
                    "enabled": True,
                    "handshake": {"server": r["server_name"], "server_port": 443},
                    "private_key": r["private_key"],
                    "short_id": [r["short_id"]],
                },
            }
        return cfg

    if proto == "trojan":
        cfg = {**base, "type": "trojan",
               "users": [{"password": c} for c in creds]}
        if st.get("reality"):
            r = st["reality"]
            cfg["tls"] = {
                "enabled": True,
                "server_name": r["server_name"],
                "reality": {
                    "enabled": True,
                    "handshake": {"server": r["server_name"], "server_port": 443},
                    "private_key": r["private_key"],
                    "short_id": [r["short_id"]],
                },
            }
        return cfg

    if proto == "hysteria2":
        return {**base, "type": "hysteria2",
                "users": [{"password": c} for c in creds],
                "tls": {
                    "enabled": True,
                    "alpn": ["h3"],
                    "certificate_path": st.get("cert_path", ""),
                    "key_path": st.get("key_path", ""),
                }}

    raise ValueError(f"未知协议: {proto}")


def generate_config() -> dict:
    c = _conn()
    rows = c.execute("SELECT * FROM inbounds WHERE enabled=1 ORDER BY port").fetchall()
    c.close()
    inbounds = []
    for r in rows:
        d = row_to_dict(r)
        d["settings"] = json.loads(d["settings"] or "{}")
        inbounds.append(_inbound_config(d))

    return {
        "log": {"level": "info", "output": LOG_PATH, "timestamp": True},
        "inbounds": inbounds,
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "experimental": {
            "clash_api": {
                "external_controller": f"127.0.0.1:{CLASH_PORT}",
                "secret": get_setting("clash_api_secret"),
            }
        },
    }


def write_config() -> tuple:
    ensure_binary()
    cfg = generate_config()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return check_config()


def check_config() -> tuple:
    ensure_binary()
    p = subprocess.run([bin_path(), "check", "-c", CONFIG_PATH],
                       capture_output=True, text=True, timeout=30)
    ok = p.returncode == 0
    msg = (p.stdout + p.stderr).strip()[-500:] or "ok"
    return ok, msg


def read_config() -> dict:
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}
