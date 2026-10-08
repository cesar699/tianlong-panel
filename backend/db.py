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
    CREATE TABLE IF NOT EXISTS traffic_history (
        ts INTEGER NOT NULL,
        inbound_id TEXT NOT NULL,
        up_bytes INTEGER NOT NULL DEFAULT 0,
        down_bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (ts, inbound_id)
    );
    CREATE INDEX IF NOT EXISTS idx_th_ts ON traffic_history(ts);
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor TEXT NOT NULL DEFAULT 'admin',
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
    """)
    defaults = {
        "admin_password_hash": _hash("admin"),
        "password_changed": "0",
        "public_host": "",
        "clash_api_secret": secrets.token_urlsafe(16),
        # 外部接管模式：sing-box 安装位置（面板不安装，只接管）
        "sb_binary": "",                                    # 空=自动探测
        "sb_config": "/etc/sing-box/config.json",
        "sb_service": "sing-box",
        "takeover_done": "0",
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


def audit_log(actor: str, action: str, detail: str = ""):
    """面板操作审计"""
    c = _conn()
    c.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)",
              (time.time(), actor, action, detail))
    # 只保留最近 2000 条
    c.execute("DELETE FROM audit_log WHERE id NOT IN "
              "(SELECT id FROM audit_log ORDER BY id DESC LIMIT 2000)")
    c.commit()
    c.close()


def list_audit(limit: int = 100) -> list:
    c = _conn()
    rows = c.execute("SELECT ts, actor, action, detail FROM audit_log "
                     "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def record_traffic_minute(ts_min: int, rows: list):
    """写入一分钟粒度的流量：rows=[(inbound_id, up, down)]"""
    c = _conn()
    c.executemany(
        "INSERT INTO traffic_history(ts, inbound_id, up_bytes, down_bytes)"
        " VALUES(?,?,?,?) ON CONFLICT(ts, inbound_id) DO UPDATE SET"
        " up_bytes=up_bytes+excluded.up_bytes,"
        " down_bytes=down_bytes+excluded.down_bytes",
        [(ts_min, ib, up, down) for ib, up, down in rows],
    )
    # 只保留 35 天
    c.execute("DELETE FROM traffic_history WHERE ts < ?", (ts_min - 35 * 86400,))
    c.commit()
    c.close()


def traffic_history_range(since_ts: int) -> list:
    c = _conn()
    rows = c.execute(
        "SELECT ts, inbound_id, up_bytes, down_bytes FROM traffic_history"
        " WHERE ts >= ? ORDER BY ts", (since_ts,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def row_to_dict(r) -> dict:
    return dict(r) if r else None
