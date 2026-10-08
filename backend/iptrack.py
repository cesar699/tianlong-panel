"""客户端 IP 连接行为跟踪
每 5 秒轮询 sing-box clash api /connections：
  - 新连接 -> conn_log 记开始（conn_key = conn_id|start_iso 联合去重，id 可能复用）
  - 存活   -> 更新累计 up/down
  - 消失   -> 回填 ended_at / duration_s / 最终流量
conn_log 只保留 30 天。
"""
import threading
import time
from datetime import datetime

from db import conn_log_cleanup, conn_log_end, conn_log_start, conn_log_update
from stats import _api_get

POLL_INTERVAL = 5
RETENTION_DAYS = 30

_lock = threading.Lock()
_live = {}          # conn_key -> client_ip（当前存活的连接）
_started = False
_last_cleanup = 0


def _parse_start(co: dict) -> float:
    try:
        return datetime.fromisoformat(
            str(co.get("start", "")).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def _track_once(conns=None):
    """单次跟踪；conns 可注入（单元测试用），None 则调 clash api"""
    global _last_cleanup
    now = time.time()
    if conns is None:
        try:
            conns = _api_get("/connections").get("connections", [])
        except Exception:
            return  # api 不可达：本轮跳过，不误判连接结束
    seen = set()
    for co in conns:
        cid = co.get("id")
        if cid is None:
            continue
        md = co.get("metadata") or {}
        start_iso = str(co.get("start") or "")
        key = f"{cid}|{start_iso}"
        seen.add(key)
        src_ip = md.get("sourceIP") or ""
        mtype = md.get("type", "")
        tag = mtype.split("/", 1)[1] if "/" in mtype else ""
        dst_host = md.get("host") or ""
        dst_ip = md.get("destinationIP") or ""
        try:
            dst_port = int(md.get("destinationPort") or 0)
        except Exception:
            dst_port = 0
        up = int(co.get("upload", 0) or 0)
        down = int(co.get("download", 0) or 0)
        with _lock:
            is_new = key not in _live
            _live[key] = (src_ip, up, down)
        if is_new:
            conn_log_start(key, src_ip, tag, dst_host, dst_ip, dst_port,
                           _parse_start(co))
            conn_log_update(key, up, down)
        else:
            conn_log_update(key, up, down)
    # 消失的连接 -> 用最后一次看到的流量回填结束
    with _lock:
        gone = [(k, v[1], v[2]) for k, v in _live.items() if k not in seen]
        for k, _, _ in gone:
            del _live[k]
    for k, up, down in gone:
        conn_log_end(k, up, down, now)
    # 定时清理（每小时一次）
    if now - _last_cleanup > 3600:
        _last_cleanup = now
        try:
            conn_log_cleanup(RETENTION_DAYS)
        except Exception:
            pass


def live_ips() -> set:
    """当前在线的客户端 IP 集合"""
    with _lock:
        return {v[0] for v in _live.values()}


def _loop():
    while True:
        try:
            _track_once()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


def start_tracker():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="ip-tracker").start()
