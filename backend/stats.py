"""实时流量统计（探针大屏数据源）
轮询 sing-box clash api：
  - /traffic (SSE)：总量实时速率
  - /connections：按 metadata.type="inboundType/inboundTag" 归因到入站
入站累计流量持久化到 sqlite；最近 60 秒速率曲线保留在内存。
"""
import json
import socket
import threading
import time
from datetime import datetime, timezone

from db import _conn, get_setting, record_traffic_minute

CLASH_PORT = 19090
HISTORY_LEN = 60

_lock = threading.Lock()
_speed_history = []          # [(ts, up_rate, down_rate)]
_peak = {"up": 0, "down": 0}
_current = {"up": 0, "down": 0}
_last_totals = {"up": 0, "down": 0, "ts": 0}
_conn_seen = {}             # conn_id -> (upload, download)
_inbound_rates = {}         # tag -> {"up": rate, "down": rate}
_inbound_conns = {}         # tag -> active connection count
_panel_pid = None


def _secret() -> str:
    return get_setting("clash_api_secret")


def _api_get(path: str, timeout: int = 3):
    """简单 HTTP GET（clash api 无 TLS）"""
    s = socket.create_connection(("127.0.0.1", CLASH_PORT), timeout=timeout)
    try:
        s.sendall(f"GET {path} HTTP/1.0\r\nAuthorization: Bearer {_secret()}\r\n\r\n".encode())
        s.settimeout(timeout)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 2_000_000:
                break
        text = data.decode("utf-8", errors="replace")
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else text
        return json.loads(body)
    finally:
        s.close()


def _traffic_sse(timeout: int = 3):
    """读 /traffic 首个数据包 -> (up, down) 累计值。
    sing-box 返回裸 JSON 行（{"up":..,"down":..}），兼容 data: 前缀的 SSE 格式。"""
    s = socket.create_connection(("127.0.0.1", CLASH_PORT), timeout=timeout)
    try:
        s.sendall(f"GET /traffic HTTP/1.0\r\nAuthorization: Bearer {_secret()}\r\n\r\n".encode())
        s.settimeout(timeout)
        data = s.recv(4096).decode(errors="replace")
        for line in data.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    d = json.loads(line)
                    return int(d.get("up", 0)), int(d.get("down", 0))
                except Exception:
                    continue
    finally:
        s.close()
    return None


def _api_delete(path: str, timeout: int = 5) -> bool:
    s = socket.create_connection(("127.0.0.1", CLASH_PORT), timeout=timeout)
    try:
        s.sendall(f"DELETE {path} HTTP/1.0\r\nAuthorization: Bearer {_secret()}\r\n\r\n".encode())
        s.settimeout(timeout)
        data = s.recv(4096).decode(errors="replace")
        return " 200 " in data.split("\r\n", 1)[0]
    finally:
        s.close()


def list_connections() -> list:
    """当前活跃连接：客户端IP、目标、协议/入站、上下行、时长"""
    try:
        conns = _api_get("/connections").get("connections", [])
    except Exception:
        return []
    now = time.time()
    out = []
    for co in conns:
        md = co.get("metadata") or {}
        mtype = md.get("type", "")
        proto, _, tag = mtype.partition("/")
        dst_host = md.get("host") or ""
        dst_ip = md.get("destinationIP") or ""
        dst_port = md.get("destinationPort") or ""
        dst = f"{dst_host or dst_ip}:{dst_port}" if (dst_host or dst_ip) else ""
        try:
            start = datetime.fromisoformat(co.get("start", "").replace("Z", "+00:00")).timestamp()
            dur = max(int(now - start), 0)
        except Exception:
            dur = 0
        out.append({
            "id": co.get("id"),
            "src": f"{md.get('sourceIP', '')}:{md.get('sourcePort', '')}",
            "dst": dst,
            "protocol": proto,
            "inbound": tag,
            "up": int(co.get("upload", 0)),
            "down": int(co.get("download", 0)),
            "duration": dur,
        })
    out.sort(key=lambda x: x["down"] + x["up"], reverse=True)
    return out


def close_connection(conn_id) -> bool:
    try:
        return _api_delete(f"/connections/{conn_id}")
    except Exception:
        return False


def _tag_to_inbound_id() -> dict:
    c = _conn()
    rows = c.execute("SELECT id, tag FROM inbounds").fetchall()
    c.close()
    return {r["tag"]: r["id"] for r in rows}


def _add_inbound_traffic(inbound_id: str, up: int, down: int):
    if up <= 0 and down <= 0:
        return
    c = _conn()
    c.execute(
        """INSERT INTO inbound_traffic(inbound_id, up_bytes, down_bytes, updated_at)
           VALUES(?,?,?,?) ON CONFLICT(inbound_id) DO UPDATE SET
           up_bytes=up_bytes+excluded.up_bytes,
           down_bytes=down_bytes+excluded.down_bytes,
           updated_at=excluded.updated_at""",
        (inbound_id, up, down, time.time()),
    )
    c.commit()
    c.close()


def _poll_once():
    global _last_totals
    now = time.time()
    tag2id = _tag_to_inbound_id()

    # 1) 总量速率（/traffic SSE 累计值求导）
    try:
        t = _traffic_sse()
    except Exception:
        t = None
    if t:
        up_tot, down_tot = t
        with _lock:
            prev = _last_totals
            dt = now - prev["ts"] if prev["ts"] else 0
            if dt > 0.3 and up_tot >= prev["up"] and down_tot >= prev["down"]:
                up_rate = (up_tot - prev["up"]) / dt
                down_rate = (down_tot - prev["down"]) / dt
                _current["up"], _current["down"] = up_rate, down_rate
                _peak["up"] = max(_peak["up"], up_rate)
                _peak["down"] = max(_peak["down"], down_rate)
                _speed_history.append((now, up_rate, down_rate))
                while len(_speed_history) > HISTORY_LEN:
                    _speed_history.pop(0)
            elif dt <= 0.3:
                pass
            else:
                # 计数器重置（sing-box 重启）
                _current["up"] = _current["down"] = 0
            _last_totals = {"up": up_tot, "down": down_tot, "ts": now}
    else:
        with _lock:
            # clash api 不可达：速率归零（可能 sing-box 未运行）
            if now - _last_totals.get("ts", 0) > 5:
                _current["up"] = _current["down"] = 0

    # 2) 按入站归因（/connections）
    try:
        conns = _api_get("/connections").get("connections", [])
    except Exception:
        conns = None
    if conns is None:
        return

    deltas = {}   # tag -> [up_delta, down_delta]
    counts = {}
    seen_now = set()
    with _lock:
        for co in conns:
            cid = co.get("id")
            if cid is None:
                continue
            seen_now.add(cid)
            mtype = (co.get("metadata") or {}).get("type", "")
            tag = mtype.split("/", 1)[1] if "/" in mtype else ""
            up, down = int(co.get("upload", 0)), int(co.get("download", 0))
            prev = _conn_seen.get(cid)
            if prev:
                du, dd = max(up - prev[0], 0), max(down - prev[1], 0)
            else:
                du, dd = 0, 0
            _conn_seen[cid] = (up, down)
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
                if du or dd:
                    d = deltas.setdefault(tag, [0, 0])
                    d[0] += du
                    d[1] += dd
        # 消失的连接：已在最后一次可见时结算增量，直接丢弃
        for cid in list(_conn_seen):
            if cid not in seen_now:
                del _conn_seen[cid]
        # 入站速率（按轮询间隔估算）
        for tag in list(_inbound_rates):
            _inbound_rates[tag] = {"up": 0, "down": 0}
        for tag, (du, dd) in deltas.items():
            _inbound_rates[tag] = {"up": du / 2.0, "down": dd / 2.0}
        _inbound_conns.clear()
        _inbound_conns.update(counts)

    # 3) 持久化入站累计
    for tag, (du, dd) in deltas.items():
        ib_id = tag2id.get(tag)
        if ib_id:
            _add_inbound_traffic(ib_id, du, dd)


def _loop():
    while True:
        try:
            _poll_once()
        except Exception:
            pass
        time.sleep(2)


_started = False
_hist_prev = {"ts_min": 0, "inbounds": {}, "total_up": 0, "total_down": 0}


def _history_loop():
    """每分钟把累计流量差值写入 traffic_history"""
    global _hist_prev
    while True:
        try:
            time.sleep(60 - time.time() % 60 + 1)
            ts_min = int(time.time()) // 60 * 60
            totals = inbound_traffic_totals()  # 累计值
            with _lock:
                t_up, t_down = _last_totals.get("up", 0), _last_totals.get("down", 0)
            prev = _hist_prev
            if prev["ts_min"]:
                rows = []
                for ib_id, t in totals.items():
                    pu, pd = prev["inbounds"].get(ib_id, (0, 0))
                    cu, cd = t["up"], t["down"]
                    du = cu - pu if cu >= pu else cu
                    dd = cd - pd if cd >= pd else cd
                    if du or dd:
                        rows.append((ib_id, max(du, 0), max(dd, 0)))
                # 总量（含非入站流量）单独记一条 __total__
                tu = t_up - prev["total_up"] if t_up >= prev["total_up"] else t_up
                td = t_down - prev["total_down"] if t_down >= prev["total_down"] else t_down
                rows.append(("__total__", max(tu, 0), max(td, 0)))
                if rows:
                    record_traffic_minute(ts_min, rows)
            _hist_prev = {
                "ts_min": ts_min,
                "inbounds": {k: (v["up"], v["down"]) for k, v in totals.items()},
                "total_up": t_up, "total_down": t_down,
            }
        except Exception:
            pass


_hist_started = False


def start_poller():
    global _started, _hist_started
    if _started:
        return
    _started = True
    th = threading.Thread(target=_loop, daemon=True, name="stats-poller")
    th.start()
    if not _hist_started:
        _hist_started = True
        th2 = threading.Thread(target=_history_loop, daemon=True, name="stats-history")
        th2.start()


def reset_peak():
    with _lock:
        _peak["up"] = _peak["down"] = 0


def snapshot() -> dict:
    """给 /api/dashboard 用的快照"""
    with _lock:
        hist = list(_speed_history)
        cur = dict(_current)
        peak = dict(_peak)
        rates = {k: dict(v) for k, v in _inbound_rates.items()}
        conns = dict(_inbound_conns)
    return {"history": hist, "current": cur, "peak": peak,
            "inbound_rates": rates, "inbound_conns": conns}


def inbound_traffic_totals() -> dict:
    c = _conn()
    rows = c.execute("SELECT inbound_id, up_bytes, down_bytes FROM inbound_traffic").fetchall()
    c.close()
    return {r["inbound_id"]: {"up": r["up_bytes"], "down": r["down_bytes"]} for r in rows}
