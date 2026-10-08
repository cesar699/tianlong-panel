"""config.json 生成 / 读写 / 校验（外部接管模式）

面板全量生成 config.json，写到外部路径（默认 /etc/sing-box/config.json）：
  - 每次写入前把现有文件备份为 config.json.bak
  - 写入后跑 `sing-box check -c` 校验，失败则从 .bak 回滚
"""
import json
import os
import shutil
import subprocess
import time

from db import _conn, get_setting, row_to_dict
from singbox import effective_binary, effective_config_path, require_binary

CLASH_PORT = 19090


def config_path() -> str:
    return effective_config_path()


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
            if r.get("_no_private_key"):
                raise ValueError(f"入站 {ib['tag']} 的 reality 缺少 private_key（原配置无法还原），请删除后重建该入站")
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
            if r.get("_no_private_key"):
                raise ValueError(f"入站 {ib['tag']} 的 reality 缺少 private_key（原配置无法还原），请删除后重建该入站")
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
        # 日志走 stdout，由 systemd journal 收集（面板日志页读 journalctl）
        "log": {"level": "info", "timestamp": True},
        "inbounds": inbounds,
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "experimental": {
            "clash_api": {
                # 探针固定用该端口；若你原来的配置里 clash api 端口不同，接管后会被统一成这个
                "external_controller": f"127.0.0.1:{CLASH_PORT}",
                "secret": get_setting("clash_api_secret"),
            }
        },
    }


def backup_timestamped() -> str:
    """接管时用：备份为 config.json.panel-bak-<时间戳>，返回备份路径（无文件则返回空）"""
    p = config_path()
    if not os.path.isfile(p):
        return ""
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{p}.panel-bak-{ts}"
    shutil.copy2(p, bak)
    return bak


def write_config() -> tuple:
    """写外部路径：先备份 .bak，校验失败回滚"""
    require_binary()
    p = config_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    if os.path.isfile(p):
        shutil.copy2(p, p + ".bak")
    try:
        cfg = generate_config()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        ok, msg = check_config()
    except Exception as e:
        ok, msg = False, f"生成配置异常: {e}"
    if not ok and os.path.isfile(p + ".bak"):
        shutil.copy2(p + ".bak", p)
        return False, f"校验失败，已从 .bak 回滚: {msg}"
    return ok, msg


def check_config(path: str = "") -> tuple:
    b = require_binary()
    p = path or config_path()
    pr = subprocess.run([b, "check", "-c", p],
                        capture_output=True, text=True, timeout=30)
    ok = pr.returncode == 0
    msg = (pr.stdout + pr.stderr).strip()[-500:] or "ok"
    return ok, msg


def read_config() -> dict:
    p = config_path()
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}
