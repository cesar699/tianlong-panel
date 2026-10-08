"""sing-box 外部接管模式：探测已安装的 sing-box，不下载、不自带二进制。

面板假设 sing-box 已由用户自行安装（手动 / 官方 release / 第三方脚本均可）。
"""
import os
import re
import shutil
import subprocess
import uuid as _uuid

from db import get_setting

DEFAULT_CONFIG_PATH = "/etc/sing-box/config.json"
DEFAULT_SERVICE = "sing-box"
FALLBACK_BINARY = "/usr/local/bin/sing-box"


# ---------- 路径解析 ----------
def effective_binary() -> str:
    """配置优先，否则自动探测"""
    cfg = (get_setting("sb_binary") or "").strip()
    if cfg:
        return cfg
    return detect_binary_path() or ""


def effective_config_path() -> str:
    return (get_setting("sb_config") or "").strip() or DEFAULT_CONFIG_PATH


def effective_service() -> str:
    return (get_setting("sb_service") or "").strip() or DEFAULT_SERVICE


def detect_binary_path() -> str:
    """which sing-box，fallback /usr/local/bin/sing-box"""
    p = shutil.which("sing-box")
    if p and os.access(p, os.X_OK):
        return p
    if os.path.isfile(FALLBACK_BINARY) and os.access(FALLBACK_BINARY, os.X_OK):
        return FALLBACK_BINARY
    return ""


def require_binary() -> str:
    p = effective_binary()
    if not p or not (os.path.isfile(p) and os.access(p, os.X_OK)):
        raise RuntimeError(
            "未检测到 sing-box 二进制，请先安装 sing-box "
            "（https://github.com/SagerNet/sing-box/releases）后再使用面板"
        )
    return p


# ---------- 版本 / 生成工具 ----------
def detect_version() -> str:
    try:
        b = effective_binary()
        if not b:
            return ""
        out = subprocess.run([b, "version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"sing-box version (\S+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def generate_reality_keypair() -> dict:
    b = require_binary()
    out = subprocess.run([b, "generate", "reality-keypair"],
                         capture_output=True, text=True, timeout=15).stdout
    priv = re.search(r"PrivateKey:\s*(\S+)", out)
    pub = re.search(r"PublicKey:\s*(\S+)", out)
    if not priv or not pub:
        raise RuntimeError("reality keypair 生成失败: " + out[:200])
    return {"private_key": priv.group(1), "public_key": pub.group(1)}


def generate_uuid() -> str:
    return str(_uuid.uuid4())


def derive_reality_public(private_b64: str) -> str:
    """从 reality private_key（base64url、无 padding）推导 public_key（RFC 7748 X25519）"""
    import base64 as _b64
    s = (private_b64 or "").strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    priv = _b64.b64decode(s)
    if len(priv) != 32:
        raise ValueError("private_key 长度非法")
    P = 2**255 - 19
    A24 = 121665

    def cswap(sw, x2, x3, z2, z3):
        if sw:
            return x3, x2, z3, z2
        return x2, x3, z2, z3

    d = int.from_bytes(priv, "little")
    d &= ~7
    d &= ~(1 << 255)
    d |= (1 << 254)
    x1 = 9
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in range(254, -1, -1):
        k = (d >> t) & 1
        swap ^= k
        x2, x3, z2, z3 = cswap(swap, x2, x3, z2, z3)
        swap = k
        A = (x2 + z2) % P
        AA = A * A % P
        B = (x2 - z2) % P
        BB = B * B % P
        E = (AA - BB) % P
        C = (x3 + z3) % P
        D = (x3 - z3) % P
        DA = D * A % P
        CB = C * B % P
        x3 = pow(DA + CB, 2, P)
        z3 = x1 * pow(DA - CB, 2, P) % P
        x2 = AA * BB % P
        z2 = E * ((AA + A24 * E) % P) % P
    x2, x3, z2, z3 = cswap(swap, x2, x3, z2, z3)
    x2 = x2 * pow(z2, P - 2, P) % P
    raw = x2.to_bytes(32, "little")
    return _b64.urlsafe_b64encode(raw).decode().rstrip("=")


# ---------- 安装探测（给前端"探测安装"按钮） ----------
def _service_active(service: str) -> bool:
    try:
        p = subprocess.run(["systemctl", "is-active", service],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip() == "active"
    except Exception:
        return False


def detect_installation() -> dict:
    b = effective_binary()
    cfg_path = effective_config_path()
    service = effective_service()
    binary_ok = bool(b and os.path.isfile(b) and os.access(b, os.X_OK))
    return {
        "binary": b,
        "binary_ok": binary_ok,
        "version": detect_version() if binary_ok else "",
        "config_path": cfg_path,
        "config_exists": os.path.isfile(cfg_path),
        "service": service,
        "service_active": _service_active(service) if binary_ok else False,
        "installed": binary_ok,
    }
