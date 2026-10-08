"""订阅链接生成（单用户轻量版）
每个入站直接一个订阅链接 + 本地二维码（qrcode 库，绝不调外部 API）。
聚合订阅 /sub 返回所有启用入站的链接（纯文本）。
"""
import base64
import io
import json
import urllib.parse

import qrcode

from db import get_setting
from inbounds import get_inbound, list_inbounds


def public_host() -> str:
    h = get_setting("public_host", "").strip()
    return h if h else "YOUR_SERVER_IP"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _st(ib: dict) -> dict:
    s = ib.get("settings")
    return s if isinstance(s, dict) else json.loads(s or "{}")


def ss_link(ib: dict, host: str, cred: str = "") -> str:
    st = _st(ib)
    cred = cred or st.get("credential", "")
    name = urllib.parse.quote(ib["tag"])
    return f"ss://{_b64(st.get('method', 'aes-256-gcm') + ':' + cred)}@{host}:{ib['port']}#{name}"


def vmess_link(ib: dict, host: str, cred: str = "") -> str:
    st = _st(ib)
    cfg = {
        "v": "2", "ps": ib["tag"], "add": host, "port": ib["port"],
        "id": cred or st.get("credential", ""), "aid": "0", "scy": "auto",
        "net": "tcp", "type": "none", "host": "", "path": "", "tls": "",
        "sni": "", "alpn": "", "fp": "",
    }
    return "vmess://" + base64.b64encode(json.dumps(cfg).encode()).decode()


def vless_link(ib: dict, host: str, cred: str = "") -> str:
    st = _st(ib)
    name = urllib.parse.quote(ib["tag"])
    q = {"encryption": "none"}
    if st.get("reality"):
        r = st["reality"]
        q.update({"security": "reality", "sni": r["server_name"], "fp": "chrome",
                  "pbk": r["public_key"], "sid": r["short_id"],
                  "flow": st.get("flow", "xtls-rprx-vision")})
    else:
        q["security"] = "none"
    return f"vless://{cred or st.get('credential', '')}@{host}:{ib['port']}?{urllib.parse.urlencode(q)}#{name}"


def trojan_link(ib: dict, host: str, cred: str = "") -> str:
    st = _st(ib)
    name = urllib.parse.quote(ib["tag"])
    q = {}
    if st.get("reality"):
        r = st["reality"]
        q.update({"security": "reality", "sni": r["server_name"], "fp": "chrome",
                  "pbk": r["public_key"], "sid": r["short_id"]})
    else:
        q["security"] = "none"
    return f"trojan://{cred or st.get('credential', '')}@{host}:{ib['port']}?{urllib.parse.urlencode(q)}#{name}"


def hysteria2_link(ib: dict, host: str, cred: str = "") -> str:
    st = _st(ib)
    name = urllib.parse.quote(ib["tag"])
    q = {"insecure": "1", "sni": host}
    return f"hysteria2://{cred or st.get('credential', '')}@{host}:{ib['port']}?{urllib.parse.urlencode(q)}#{name}"


BUILDERS = {
    "shadowsocks": ss_link,
    "vmess": vmess_link,
    "vless": vless_link,
    "trojan": trojan_link,
    "hysteria2": hysteria2_link,
}


def inbound_link(ib_id: str, host: str = "", cred: str = "") -> dict:
    ib = get_inbound(ib_id)
    if not ib:
        raise ValueError("入站不存在")
    host = host or public_host()
    builder = BUILDERS.get(ib["protocol"])
    if not builder:
        raise ValueError(f"不支持的协议: {ib['protocol']}")
    link = builder(ib, host, cred)
    return {"protocol": ib["protocol"], "tag": ib["tag"], "port": ib["port"],
            "link": link, "qr": qr_data_uri(link)}


def qr_data_uri(text: str) -> str:
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def aggregate_links(host: str = "") -> list:
    """聚合订阅：所有启用入站的主凭证链接"""
    host = host or public_host()
    out = []
    for ib in list_inbounds():
        if not ib["enabled"]:
            continue
        builder = BUILDERS.get(ib["protocol"])
        if builder:
            out.append(builder(ib, host))
    return out
