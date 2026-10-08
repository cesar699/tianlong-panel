"""SQLite 数据层：入站、设置、会话、流量统计（单用户轻量版，无多用户表）"""
import hashlib
import os
import secrets
import sqlite3
import time

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "panel.db")


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS inbounds (
        id TEXT PRIMARY KEY,
        tag TEXT NOT NULL,
        protocol TEXT NOT NULL,
        port INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        settings TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS inbound_traffic (
        inbound_id TEXT PRIMARY KEY,
        up_bytes INTEGER NOT NULL DEFAULT 0,
        down_bytes INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    );
    """)
    defaults = {
        "admin_password_hash": _hash("admin"),
        "password_changed": "0",
        "singbox_version": "1.14.2",
        "public_host": "",
        "clash_api_secret": secrets.token_urlsafe(16),
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    c.commit()
    c.close()


def _hash(pw: str) -> str:
    return hashlib.sha256(("sbpanel:" + pw).encode()).hexdigest()


def verify_password(pw: str) -> bool:
    return get_setting("admin_password_hash") == _hash(pw)


def set_password(pw: str):
    set_setting("admin_password_hash", _hash(pw))
    set_setting("password_changed", "1")


def get_setting(key: str, default: str = "") -> str:
    c = _conn()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default


def set_setting(key: str, value: str):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    c.commit()
    c.close()


def new_id() -> str:
    return secrets.token_hex(8)


def row_to_dict(r) -> dict:
    return dict(r) if r else None
