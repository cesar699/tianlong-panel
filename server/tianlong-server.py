#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天龙面板 Server
- 接收 Agent 上报，存 SQLite
- REST API + 看板页面
- 告警引擎：阈值告警 + 磁盘写满预测 + 离线检测
- 应用层监控：HTTP / TCP / SSL 证书
- AI 运维助手：规则版开箱即用，可选接 LLM

只依赖: fastapi, uvicorn (psutil/requests 仅 agent 需要)
配置: 同目录 server.json (见 server.json.example) 或环境变量
"""
import json
import math
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "tianlong.db")

# ---------------- 配置 ----------------
def load_config():
    cfg = {}
    p = os.path.join(BASE, "server.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    g = lambda k, d: os.environ.get("TIANLONG_" + k.upper(), cfg.get(k.lower(), d))
    return {
        "secret":   g("secret", "tianlong-secret-please-change"),
        "port":     int(g("port", 8009)),
        "llm_key":  g("llm_key", ""),
        "llm_base": g("llm_base", "https://api.meta.ai/v1"),
        "llm_model":g("llm_model", "muse-spark-1.3"),
        "checks":   cfg.get("checks", []),  # [{"name":"官网","type":"http","target":"https://example.com"}]
    }

CFG = load_config()
app = FastAPI(title="天龙面板")

# 全局写锁：串行化所有 DB 写操作，彻底避免 database is locked
db_lock = threading.Lock()

# ---------------- 数据库 ----------------
def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS servers(
        name TEXT PRIMARY KEY, last_seen REAL, platform TEXT, info TEXT);
    CREATE TABLE IF NOT EXISTS metrics(
        id INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT, ts REAL,
        cpu REAL, load1 REAL, mem_pct REAL, mem_total_mb REAL, mem_used_mb REAL,
        disk_pct REAL, disk_total_gb REAL, disk_used_gb REAL,
        net_in_bps REAL, net_out_bps REAL, uptime_s REAL, top_procs TEXT);
    CREATE INDEX IF NOT EXISTS idx_m_server_ts ON metrics(server, ts);
    CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT, ts REAL,
        level TEXT, message TEXT, resolved INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS appchecks(
        name TEXT PRIMARY KEY, type TEXT, target TEXT,
        last_ok INTEGER, last_latency_ms REAL, last_msg TEXT, ssl_days REAL, checked_ts REAL);
    CREATE TABLE IF NOT EXISTS probes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT, ip TEXT,
        attempts INTEGER, ts REAL);
    CREATE INDEX IF NOT EXISTS idx_probes_ts ON probes(ts);
    CREATE INDEX IF NOT EXISTS idx_probes_ip ON probes(ip);
    CREATE TABLE IF NOT EXISTS geoip(
        ip TEXT PRIMARY KEY, country TEXT, city TEXT,
        lat REAL, lon REAL, ts REAL);
    """)
    c.commit(); c.close()

init_db()

# ---------------- 工具 ----------------
def now(): return time.time()

def fmt_bytes(bps):
    for u, f in (("B/s", 1), ("KB/s", 1024), ("MB/s", 1024**2), ("GB/s", 1024**3)):
        if bps < f * 1024 or u == "GB/s":
            return f"{bps/f:.1f} {u}"
    return f"{bps:.0f} B/s"

def fmt_dur(s):
    s = int(s); d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, _ = divmod(s, 60)
    return f"{d}天{h}小时" if d else f"{h}小时{m}分" if h else f"{m}分"

# ---------------- 上报接入 ----------------
@app.post("/api/report")
async def report(req: Request):
    body = await req.json()
    if body.get("secret") != CFG["secret"]:
        return JSONResponse({"ok": False, "msg": "secret 错误"}, status_code=403)
    name = (body.get("name") or "unknown")[:64]
    m = body.get("metrics", {})
    with db_lock:
        c = db()
        c.execute("INSERT OR REPLACE INTO servers(name,last_seen,platform,info) VALUES(?,?,?,?)",
                  (name, now(), m.get("platform", ""), ""))
        c.execute("""INSERT INTO metrics(server,ts,cpu,load1,mem_pct,mem_total_mb,mem_used_mb,
                     disk_pct,disk_total_gb,disk_used_gb,net_in_bps,net_out_bps,uptime_s,top_procs)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (name, m.get("ts", now()), m.get("cpu_pct"), m.get("load1"),
                   m.get("mem_pct"), m.get("mem_total_mb"), m.get("mem_used_mb"),
                   m.get("disk_pct"), m.get("disk_total_gb"), m.get("disk_used_gb"),
                   m.get("net_in_bps"), m.get("net_out_bps"), m.get("uptime_s"),
                   json.dumps(m.get("top_procs", []), ensure_ascii=False)))
        # 只保留 7 天
        c.execute("DELETE FROM metrics WHERE ts < ?", (now() - 7*86400,))
        # SSH 探测记录
        for p in (m.get("ssh_probes") or []):
            try:
                c.execute("INSERT INTO probes(server,ip,attempts,ts) VALUES(?,?,?,?)",
                          (name, p["ip"][:45], int(p["attempts"]), now()))
            except Exception:
                pass
        c.execute("DELETE FROM probes WHERE ts < ?", (now() - 7*86400,))
        c.commit(); c.close()
    return {"ok": True}

# ---------------- 查询 API ----------------
def latest_metrics(server):
    c = db()
    r = c.execute("SELECT * FROM metrics WHERE server=? ORDER BY ts DESC LIMIT 1",
                  (server,)).fetchone()
    c.close()
    return dict(r) if r else None

@app.get("/api/servers")
def servers():
    c = db()
    rows = c.execute("SELECT * FROM servers").fetchall()
    c.close()
    out = []
    for s in rows:
        m = latest_metrics(s["name"])
        online = (now() - s["last_seen"]) < 120 if s["last_seen"] else False
        item = {"name": s["name"], "platform": s["platform"],
                "last_seen": s["last_seen"], "online": online, "metrics": None}
        if m:
            m.pop("id", None)
            m["top_procs"] = json.loads(m["top_procs"] or "[]")
            item["metrics"] = m
        out.append(item)
    return {"servers": out, "ts": now()}

@app.get("/api/metrics/{server}")
def metrics(server: str, hours: float = 24):
    c = db()
    rows = c.execute(
        "SELECT ts,cpu,mem_pct,disk_pct,net_in_bps,net_out_bps,load1 FROM metrics "
        "WHERE server=? AND ts>? ORDER BY ts",
        (server, now() - hours*3600)).fetchall()
    c.close()
    # 降采样到最多 600 点
    rows = list(rows)
    step = max(1, len(rows)//600)
    return {"points": [dict(r) for r in rows[::step]]}

@app.get("/api/alerts")
def alerts(limit: int = 50):
    c = db()
    rows = c.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return {"alerts": [dict(r) for r in rows]}

@app.get("/api/checks")
def checks():
    c = db()
    rows = c.execute("SELECT * FROM appchecks").fetchall()
    c.close()
    return {"checks": [dict(r) for r in rows]}

@app.post("/api/checks")
async def add_check(req: Request):
    body = await req.json()
    name, typ, target = body.get("name"), body.get("type"), body.get("target")
    if not (name and typ and target) or typ not in ("http", "tcp"):
        return JSONResponse({"ok": False, "msg": "参数错误: name/type(http|tcp)/target"}, status_code=400)
    with db_lock:
        c = db()
        c.execute("INSERT OR REPLACE INTO appchecks(name,type,target,last_ok,checked_ts) VALUES(?,?,?,NULL,NULL)",
                  (name, typ, target))
        c.commit(); c.close()
    return {"ok": True}

# ---------------- 告警引擎 ----------------
_active = {}  # (server, rule) -> alert_id

def push_alert(server, rule, level, message):
    key = (server, rule)
    if key in _active:
        return
    with db_lock:
        c = db()
        cur = c.execute("INSERT INTO alerts(server,ts,level,message) VALUES(?,?,?,?)",
                        (server, now(), level, message))
        c.commit(); c.close()
        _active[key] = cur.lastrowid

def resolve_alert(server, rule):
    key = (server, rule)
    if key not in _active:
        return
    with db_lock:
        c = db()
        c.execute("UPDATE alerts SET resolved=1 WHERE id=?", (_active[key],))
        c.commit(); c.close()
        del _active[key]

def disk_eta_days(server):
    """线性回归预测磁盘写满天数，None 表示无法预测/在下降"""
    c = db()
    rows = c.execute("SELECT ts,disk_used_gb,disk_total_gb FROM metrics WHERE server=? AND ts>? ORDER BY ts",
                     (server, now()-6*3600)).fetchall()
    c.close()
    if len(rows) < 10:
        return None
    n = len(rows)
    xs = [r["ts"] for r in rows]; ys = [r["disk_used_gb"] for r in rows]
    mx, my = sum(xs)/n, sum(ys)/n
    den = sum((x-mx)**2 for x in xs)
    if den == 0: return None
    slope = sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / den  # GB per second
    if slope <= 0: return None
    total = rows[-1]["disk_total_gb"]
    remain = total - ys[-1]
    return remain / (slope*86400)

def alert_loop():
    while True:
        try:
            c = db()
            servers = [dict(r) for r in c.execute("SELECT * FROM servers").fetchall()]
            c.close()
            for s in servers:
                name = s["name"]
                # 离线
                if not s["last_seen"] or now() - s["last_seen"] > 120:
                    push_alert(name, "offline", "critical", f"{name} 离线超过 2 分钟（最后上报 {datetime.fromtimestamp(s['last_seen'] or 0).strftime('%H:%M:%S') if s['last_seen'] else '从未'}）")
                    continue
                else:
                    resolve_alert(name, "offline")
                m = latest_metrics(name)
                if not m: continue
                # CPU
                if m["cpu"] and m["cpu"] > 90:
                    top = ""
                    try:
                        procs = json.loads(m["top_procs"] or "[]")
                        if procs: top = f"，最高进程 {procs[0]['name']}({procs[0]['cpu']}%)"
                    except Exception: pass
                    push_alert(name, "cpu", "warning", f"{name} CPU {m['cpu']}% 超过 90%{top}")
                else:
                    resolve_alert(name, "cpu")
                # 内存
                if m["mem_pct"] and m["mem_pct"] > 90:
                    push_alert(name, "mem", "warning", f"{name} 内存 {m['mem_pct']}% 超过 90%")
                else:
                    resolve_alert(name, "mem")
                # 磁盘
                if m["disk_pct"] and m["disk_pct"] > 92:
                    push_alert(name, "disk", "critical", f"{name} 磁盘 {m['disk_pct']}% 超过 92%")
                else:
                    resolve_alert(name, "disk")
                # 磁盘写满预测
                eta = disk_eta_days(name)
                if eta is not None and eta < 7:
                    push_alert(name, "disk_eta", "warning",
                               f"{name} 按当前增速约 {eta:.1f} 天后磁盘写满（预测）")
                else:
                    resolve_alert(name, "disk_eta")
                # SSH 爆破检测：10 分钟内单个 IP 尝试超 100 次
                c2 = db()
                br = c2.execute("""
                    SELECT ip, SUM(attempts) AS n FROM probes
                    WHERE server=? AND ts>? GROUP BY ip ORDER BY n DESC LIMIT 1""",
                    (name, now()-600)).fetchone()
                c2.close()
                if br and br["n"] and br["n"] > 100:
                    push_alert(name, "ssh_bf", "critical",
                               f"{name} 疑似遭 SSH 爆破：{br['ip']} 10分钟内尝试 {br['n']} 次")
                else:
                    resolve_alert(name, "ssh_bf")
        except Exception as e:
            print("告警引擎异常:", e)
        time.sleep(60)

# ---------------- 应用层监控 ----------------
def check_one(name, typ, target):
    t0 = time.time()
    try:
        if typ == "http":
            req = urllib.request.Request(target, headers={"User-Agent": "TianlongPanel/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ms = (time.time()-t0)*1000
                ssl_days = None
                if target.startswith("https://"):
                    host = urllib.parse.urlparse(target).hostname
                    ctx = ssl.create_default_context()
                    with ctx.wrap_socket(socket.socket(), server_hostname=host) as s:
                        s.settimeout(10); s.connect((host, 443))
                        cert = s.getpeercert()
                        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
                        ssl_days = (exp - datetime.utcnow()).total_seconds()/86400
                return True, ms, f"HTTP {r.status}", ssl_days
        elif typ == "tcp":
            host, _, port = target.partition(":")
            with socket.create_connection((host, int(port or 80)), timeout=10):
                return True, (time.time()-t0)*1000, "端口通", None
    except Exception as e:
        return False, (time.time()-t0)*1000, f"失败: {e}"[:120], None
    return False, 0, "未知", None

def check_loop():
    while True:
        try:
            for chk in CFG["checks"]:
                ok, ms, msg, ssl_days = check_one(chk["name"], chk["type"], chk["target"])
                with db_lock:
                    c = db()
                    c.execute("""INSERT OR REPLACE INTO appchecks
                                 (name,type,target,last_ok,last_latency_ms,last_msg,ssl_days,checked_ts)
                                 VALUES(?,?,?,?,?,?,?,?)""",
                              (chk["name"], chk["type"], chk["target"], 1 if ok else 0,
                               round(ms,1), msg, ssl_days, now()))
                    c.commit(); c.close()
                if not ok:
                    push_alert(chk["name"], "appcheck", "critical",
                               f"应用监控 [{chk['name']}] 异常: {msg}")
                else:
                    resolve_alert(chk["name"], "appcheck")
                    if ssl_days is not None and ssl_days < 14:
                        push_alert(chk["name"], "ssl", "warning",
                                   f"[{chk['name']}] SSL 证书还有 {ssl_days:.0f} 天过期")
                    else:
                        resolve_alert(chk["name"], "ssl")
            # 数据库里后加的检查也跑
            c = db()
            extra = [dict(r) for r in c.execute("SELECT name,type,target FROM appchecks").fetchall()]
            c.close()
            known = {x["name"] for x in CFG["checks"]}
            for e in extra:
                if e["name"] in known: continue
                ok, ms, msg, ssl_days = check_one(e["name"], e["type"], e["target"])
                with db_lock:
                    c = db()
                    c.execute("UPDATE appchecks SET last_ok=?,last_latency_ms=?,last_msg=?,ssl_days=?,checked_ts=? WHERE name=?",
                              (1 if ok else 0, round(ms,1), msg, ssl_days, now(), e["name"]))
                    c.commit(); c.close()
        except Exception as e:
            print("应用监控异常:", e)
        time.sleep(60)

# ---------------- IP 归属地 ----------------
def geo_lookup_batch(ips):
    """用 ip-api.com 批量查询归属地（免费 45 次/分钟，batch 一次算 1 次）"""
    try:
        body = json.dumps([{"query": ip} for ip in ips]).encode()
        url = ("http://ip-api.com/batch?fields=status,message,country,city,lat,lon,query")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        print("归属地查询失败:", e)
        return []

def geo_loop():
    while True:
        try:
            c = db()
            rows = c.execute("""
                SELECT DISTINCT ip FROM probes
                WHERE ts > ? AND ip NOT IN (SELECT ip FROM geoip)
                LIMIT 90""", (now() - 24*3600,)).fetchall()
            c.close()
            ips = [r["ip"] for r in rows]
            # 跳过内网 IP
            ips = [ip for ip in ips
                   if not (ip.startswith("10.") or ip.startswith("192.168.")
                           or ip.startswith("172.16.") or ip == "127.0.0.1")]
            if ips:
                with db_lock:
                    c = db()
                    for item in geo_lookup_batch(ips):
                        if item.get("status") == "success":
                            c.execute("""INSERT OR REPLACE INTO geoip
                                         (ip,country,city,lat,lon,ts) VALUES(?,?,?,?,?,?)""",
                                      (item["query"], item.get("country", ""),
                                       item.get("city", ""), item.get("lat"),
                                       item.get("lon"), now()))
                    c.commit(); c.close()
        except Exception as e:
            print("归属地线程异常:", e)
        time.sleep(120)

# ---------------- 攻击聚合 API ----------------
@app.get("/api/attacks")
def attacks(hours: float = 24, limit: int = 200):
    c = db()
    rows = c.execute("""
        SELECT ip, SUM(attempts) AS attempts,
               GROUP_CONCAT(DISTINCT server) AS servers, MAX(ts) AS last_seen
        FROM probes WHERE ts > ? GROUP BY ip ORDER BY attempts DESC LIMIT ?""",
        (now() - hours*3600, limit)).fetchall()
    out = []
    for r in rows:
        g = c.execute("SELECT country,city,lat,lon FROM geoip WHERE ip=?",
                      (r["ip"],)).fetchone()
        out.append({
            "ip": r["ip"], "attempts": r["attempts"],
            "servers": (r["servers"] or "").split(","),
            "last_seen": r["last_seen"],
            "country": g["country"] if g else "",
            "city": g["city"] if g else "",
            "lat": g["lat"] if g else None,
            "lon": g["lon"] if g else None,
        })
    c.close()
    return {"attacks": out, "ts": now()}

@app.get("/api/agent-install")
def agent_install():
    """返回 agent 一键安装所需的密钥和脚本地址（看板据此拼出复制即用的命令）"""
    return {"secret": CFG["secret"],
            "script": "https://raw.githubusercontent.com/cesar699/tianlong-panel/main/agent/install-agent.sh"}

@app.get("/api/probes/{server}")
def probes(server: str):
    """某台服务器最近一次上报的探测 IP"""
    c = db()
    r = c.execute("SELECT MAX(ts) AS mts FROM probes WHERE server=?", (server,)).fetchone()
    out = []
    if r and r["mts"]:
        rows = c.execute(
            "SELECT ip,attempts FROM probes WHERE server=? AND ts>? ORDER BY attempts DESC LIMIT 20",
            (server, r["mts"] - 120)).fetchall()
        for x in rows:
            g = c.execute("SELECT country,city FROM geoip WHERE ip=?", (x["ip"],)).fetchone()
            out.append({"ip": x["ip"], "attempts": x["attempts"],
                        "country": g["country"] if g else "",
                        "city": g["city"] if g else ""})
    c.close()
    return {"probes": out}

# ---------------- AI 运维助手 ----------------
def build_context():
    c = db()
    srvs = [dict(r) for r in c.execute("SELECT * FROM servers").fetchall()]
    alts = [dict(r) for r in c.execute(
        "SELECT * FROM alerts WHERE resolved=0 ORDER BY ts DESC LIMIT 10").fetchall()]
    chks = [dict(r) for r in c.execute("SELECT * FROM appchecks").fetchall()]
    atk = [dict(r) for r in c.execute("""
        SELECT ip, SUM(attempts) AS n FROM probes
        WHERE ts > ? GROUP BY ip ORDER BY n DESC LIMIT 5""", (now()-24*3600,)).fetchall()]
    c.close()
    lines = []
    for s in srvs:
        m = latest_metrics(s["name"])
        online = "在线" if s["last_seen"] and now()-s["last_seen"] < 120 else "离线"
        if m:
            lines.append(f"- {s['name']}({online}): CPU {m['cpu']}% 内存 {m['mem_pct']}% "
                         f"磁盘 {m['disk_pct']}% 负载 {m['load1']} 运行 {fmt_dur(m['uptime_s'] or 0)}")
        else:
            lines.append(f"- {s['name']}({online}): 暂无数据")
    ctx = "服务器状态:\n" + ("\n".join(lines) if lines else "(无服务器)")
    if alts:
        ctx += "\n未处理告警:\n" + "\n".join(f"- [{a['level']}] {a['message']}" for a in alts)
    if chks:
        ctx += "\n应用监控:\n" + "\n".join(
            f"- {x['name']}: {'正常' if x['last_ok'] else '异常'}({x['last_msg']})" for x in chks)
    if atk:
        ctx += "\n24h 内扫描 22 端口最多的 IP:\n" + "\n".join(
            f"- {a['ip']}: {a['n']} 次" for a in atk)
    return ctx

def rule_answer(q):
    ctx = build_context()
    ql = q.lower()
    c = db()
    srvs = [dict(r) for r in c.execute("SELECT * FROM servers").fetchall()]
    c.close()
    stats = [(s["name"], latest_metrics(s["name"])) for s in srvs]
    stats = [(n, m) for n, m in stats if m]

    def top_by(key):
        return max(stats, key=lambda x: (x[1].get(key) or 0)) if stats else (None, None)

    if any(k in q for k in ("卡", "慢", "cpu", "负载", "最忙")):
        n, m = top_by("cpu")
        if n: return f"当前 CPU 最高的是 {n}（{m['cpu']}%），1分钟负载 {m['load1']}。\n\n{ctx}"
        return "暂无服务器数据。\n\n" + ctx
    if any(k in q for k in ("磁盘", "硬盘", "满", "空间")):
        n, m = top_by("disk_pct")
        if n:
            eta = disk_eta_days(n)
            extra = f"按当前增速约 {eta:.1f} 天后写满。" if eta else "增速平稳，暂无写满风险。"
            return f"{n} 磁盘用了 {m['disk_pct']}%（{m['disk_used_gb']}/{m['disk_total_gb']}GB）。{extra}\n\n{ctx}"
        return "暂无服务器数据。\n\n" + ctx
    if any(k in q for k in ("内存", "ram")):
        n, m = top_by("mem_pct")
        if n: return f"{n} 内存用了 {m['mem_pct']}%（{m['mem_used_mb']}/{m['mem_total_mb']}MB）。\n\n{ctx}"
        return "暂无服务器数据。\n\n" + ctx
    if any(k in q for k in ("告警", "报警", "异常", "问题")):
        c = db()
        alts = [dict(r) for r in c.execute(
            "SELECT * FROM alerts WHERE resolved=0 ORDER BY ts DESC").fetchall()]
        c.close()
        if not alts: return "目前没有未处理的告警，一切正常 ✅\n\n" + ctx
        return "未处理告警:\n" + "\n".join(f"• [{a['level']}] {a['message']}" for a in alts) + "\n\n" + ctx
    if any(k in q for k in ("进程",)):
        if stats:
            n, m = stats[0]
            try: procs = json.loads(m["top_procs"] or "[]")
            except Exception: procs = []
            lines = "\n".join(f"• {p['name']} (pid {p['pid']}): CPU {p['cpu']}% MEM {p['mem']}%" for p in procs[:5])
            return f"{n} 当前最占资源的进程:\n{lines}\n\n{ctx}"
        return "暂无服务器数据。\n\n" + ctx
    # 默认：整体摘要
    return "当前总体状态:\n" + ctx

def llm_answer(q, ctx):
    import urllib.request
    sys_prompt = ("你是天龙面板的 AI 运维助手。用简洁中文回答用户关于服务器运维的问题，"
                  "基于下面给出的实时监控数据，不要编造数据中没有的信息。")
    body = json.dumps({
        "model": CFG["llm_model"],
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"监控数据:\n{ctx}\n\n用户问题: {q}"},
        ],
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(CFG["llm_base"].rstrip("/") + "/chat/completions",
                                 data=body,
                                 headers={"Authorization": f"Bearer {CFG['llm_key']}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]

@app.post("/api/ask")
async def ask(req: Request):
    body = await req.json()
    q = (body.get("question") or "").strip()
    if not q:
        return JSONResponse({"ok": False, "msg": "问题不能为空"}, status_code=400)
    ctx = build_context()
    if CFG["llm_key"]:
        try:
            return {"ok": True, "answer": llm_answer(q, ctx), "source": "llm"}
        except Exception as e:
            return {"ok": True, "answer": f"LLM 调用失败，回退到规则回答: {e}\n\n" + rule_answer(q),
                    "source": "rules"}
    return {"ok": True, "answer": rule_answer(q), "source": "rules"}

# ---------------- 看板 ----------------
@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "dashboard.html"))

@app.get("/map")
def attack_map():
    return FileResponse(os.path.join(BASE, "map.html"))

@app.get("/world.svg")
def world_svg():
    return FileResponse(os.path.join(BASE, "world.svg"), media_type="image/svg+xml")

# ---------------- 启动 ----------------
if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=alert_loop, daemon=True).start()
    threading.Thread(target=check_loop, daemon=True).start()
    threading.Thread(target=geo_loop, daemon=True).start()
    print(f"天龙面板 Server 启动: http://0.0.0.0:{CFG['port']}")
    uvicorn.run(app, host="0.0.0.0", port=CFG["port"], log_level="warning")
