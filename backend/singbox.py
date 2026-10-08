"""sing-box 二进制管理：下载官方 release、版本检测"""
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

from db import DATA_DIR, get_setting, set_setting

BIN_PATH = os.path.join(DATA_DIR, "sing-box")
GITHUB_API = "https://api.github.com/repos/SagerNet/sing-box/releases"


def bin_path() -> str:
    return BIN_PATH


def binary_exists() -> bool:
    return os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK)


def detect_version() -> str:
    """运行 sing-box version 解析版本号"""
    if not binary_exists():
        return ""
    try:
        out = subprocess.run([BIN_PATH, "version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"sing-box version (\S+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeError(f"不支持的架构: {m}")


def download(version: str = "") -> str:
    """从 SagerNet 官方 GitHub releases 下载二进制，返回版本号"""
    version = version or get_setting("singbox_version", "1.14.2")
    version = version.lstrip("v")
    arch = _arch()
    fname = f"sing-box-{version}-linux-{arch}.tar.gz"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{version}/{fname}"

    tmp = tempfile.mkdtemp()
    tgz = os.path.join(tmp, fname)
    req = urllib.request.Request(url, headers={"User-Agent": "singbox-panel"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tgz, "wb") as f:
        shutil.copyfileobj(r, f)
    with tarfile.open(tgz) as t:
        # 安全解压：只取 sing-box 可执行文件
        member = next((m for m in t.getmembers() if m.name.endswith("/sing-box") and m.isfile()), None)
        if not member:
            raise RuntimeError("压缩包中未找到 sing-box 二进制")
        src = t.extractfile(member)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(BIN_PATH, "wb") as f:
            shutil.copyfileobj(src, f)
    os.chmod(BIN_PATH, 0o755)
    shutil.rmtree(tmp, ignore_errors=True)
    set_setting("singbox_version", version)
    return version


def ensure_binary() -> str:
    """确保二进制存在，不存在则下载；返回版本号"""
    if not binary_exists():
        download()
    return detect_version()


def generate_reality_keypair() -> dict:
    """调用 sing-box generate reality-keypair"""
    ensure_binary()
    out = subprocess.run([BIN_PATH, "generate", "reality-keypair"],
                         capture_output=True, text=True, timeout=15).stdout
    priv = re.search(r"PrivateKey:\s*(\S+)", out)
    pub = re.search(r"PublicKey:\s*(\S+)", out)
    if not priv or not pub:
        raise RuntimeError("reality keypair 生成失败: " + out[:200])
    return {"private_key": priv.group(1), "public_key": pub.group(1)}


def generate_uuid() -> str:
    ensure_binary()
    out = subprocess.run([BIN_PATH, "generate", "uuid"],
                         capture_output=True, text=True, timeout=15).stdout.strip()
    return out.split()[0]
