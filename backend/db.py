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
    CREATE TABLE IF NOT EXISTS conn_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conn_key TEXT NOT NULL UNIQUE,
        client_ip TEXT NOT NULL,
        inbound_tag TEXT NOT NULL DEFAULT '',
        dst_host TEXT NOT NULL DEFAULT '',
        dst_ip TEXT NOT NULL DEFAULT '',
        dst_port INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL,
        ended_at REAL,
        duration_s INTEGER NOT NULL DEFAULT 0,
        up_bytes INTEGER NOT NULL DEFAULT 0,
        down_bytes INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_clog_ip ON conn_log(client_ip);
    CREATE INDEX IF NOT EXISTS idx_clog_started ON conn_log(started_at);
    CREATE INDEX IF NOT EXISTS idx_clog_live ON conn_log(ended_at);
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


# ---------- 连接行为日志（IP 监控） ----------

def conn_log_start(conn_key: str, client_ip: str, inbound_tag: str,
                   dst_host: str, dst_ip: str, dst_port: int, started_at: float):
    """新连接开始（conn_key = conn_id|start_iso，重复插入忽略）"""
    c = _conn()
    c.execute(
        """INSERT OR IGNORE INTO conn_log
           (conn_key, client_ip, inbound_tag, dst_host, dst_ip, dst_port, started_at)
           VALUES(?,?,?,?,?,?,?)""",
        (conn_key, client_ip, inbound_tag, dst_host, dst_ip, dst_port, started_at))
    c.commit()
    c.close()


def conn_log_update(conn_key: str, up_bytes: int, down_bytes: int):
    """更新存活连接的累计流量"""
    c = _conn()
    c.execute("UPDATE conn_log SET up_bytes=?, down_bytes=? "
              "WHERE conn_key=? AND ended_at IS NULL",
              (up_bytes, down_bytes, conn_key))
    c.commit()
    c.close()


def conn_log_end(conn_key: str, up_bytes: int, down_bytes: int, ended_at: float):
    """连接结束：回填结束时间、时长、最终流量"""
    c = _conn()
    r = c.execute("SELECT started_at FROM conn_log WHERE conn_key=? AND ended_at IS NULL",
                  (conn_key,)).fetchone()
    if r:
        dur = max(int(ended_at - r["started_at"]), 0)
        c.execute("UPDATE conn_log SET ended_at=?, duration_s=?, up_bytes=?, down_bytes=? "
                  "WHERE conn_key=?", (ended_at, dur, up_bytes, down_bytes, conn_key))
        c.commit()
    c.close()


def conn_log_cleanup(days: int = 30):
    """只保留最近 N 天的连接记录"""
    c = _conn()
    c.execute("DELETE FROM conn_log WHERE started_at < ?",
              (time.time() - days * 86400,))
    c.commit()
    c.close()


def ip_aggregate(live_ips: set) -> list:
    """IP 聚合：总流量、连接次数、累计时长、首次/末次活跃"""
    c = _conn()
    rows = c.execute(
        """SELECT client_ip,
                  COUNT(*) AS conns,
                  COALESCE(SUM(up_bytes),0) AS up,
                  COALESCE(SUM(down_bytes),0) AS down,
                  COALESCE(SUM(duration_s),0) AS dur_ended,
                  MIN(started_at) AS first_seen,
                  MAX(started_at) AS last_seen
           FROM conn_log GROUP BY client_ip""").fetchall()
    c.close()
    now = time.time()
    out = []
    for r in rows:
        ip = r["client_ip"]
        # 存活连接的时长按 now-started_at 实时计算
        extra = 0
        if ip in live_ips:
            c2 = _conn()
            lr = c2.execute("SELECT COALESCE(SUM(? - started_at),0) FROM conn_log "
                            "WHERE client_ip=? AND ended_at IS NULL", (now, ip)).fetchone()
            c2.close()
            extra = int(lr[0] or 0)
        out.append({
            "ip": ip,
            "conns": r["conns"],
            "up": r["up"],
            "down": r["down"],
            "total": r["up"] + r["down"],
            "duration": r["dur_ended"] + extra,
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "online": ip in live_ips,
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


def ip_history(ip: str, limit: int = 50, offset: int = 0) -> list:
    c = _conn()
    rows = c.execute(
        """SELECT client_ip, inbound_tag, dst_host, dst_ip, dst_port,
                  started_at, ended_at, duration_s, up_bytes, down_bytes
           FROM conn_log WHERE client_ip=? ORDER BY started_at DESC LIMIT ? OFFSET ?""",
        (ip, limit, offset)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def ip_destinations(ip: str, limit: int = 20) -> list:
    """该 IP 访问过的目标 Top：host:port（无 host 则用 ip:port）、次数、流量"""
    c = _conn()
    rows = c.execute(
        """SELECT COALESCE(NULLIF(dst_host,''), dst_ip) || ':' || dst_port AS dst,
                  COUNT(*) AS conns,
                  COALESCE(SUM(up_bytes),0) AS up,
                  COALESCE(SUM(down_bytes),0) AS down
           FROM conn_log WHERE client_ip=? GROUP BY dst
           ORDER BY down + up DESC LIMIT ?""", (ip, limit)).fetchall()
    c.close()
    return [{"dst": r["dst"], "conns": r["conns"], "up": r["up"],
             "down": r["down"], "total": r["up"] + r["down"]} for r in rows]


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
