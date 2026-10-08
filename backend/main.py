"""sing-box 可视化管理面板 - FastAPI 入口（单用户轻量版）"""
import os
import secrets
import time

import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import config_mgr
import inbounds as ib_mod
import process as proc_mod
import singbox as sb_mod
import stats as stats_mod
import subscribe as sub_mod
import takeover as takeover_mod
from db import (_conn, audit_log, get_setting, init_db, list_audit, set_password,
                set_setting, traffic_history_range, verify_password)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

app = FastAPI(title="sing-box 面板")


# ---------- 鉴权 ----------
def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    c = _conn()
    c.execute("INSERT INTO sessions(token, created_at) VALUES(?,?)", (token, time.time()))
    # 只保留最近 20 个会话
    c.execute("DELETE FROM sessions WHERE token NOT IN (SELECT token FROM sessions ORDER BY created_at DESC LIMIT 20)")
    c.commit()
    c.close()
    return token


def _valid_session(token: str) -> bool:
    if not token:
        return False
    c = _conn()
    r = c.execute("SELECT token FROM sessions WHERE token=?", (token,)).fetchone()
    c.close()
    return r is not None


def _drop_session(token: str):
    c = _conn()
    c.execute("DELETE FROM sessions WHERE token=?", (token,))
    c.commit()
    c.close()


async def auth(authorization: str = Header("")) -> str:
    token = authorization.replace("Bearer ", "").strip()
    if not _valid_session(token):
        raise HTTPException(401, "未登录或登录已过期")
    return token


class LoginBody(BaseModel):
    password: str


class ChangePwBody(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/login")
def login(b: LoginBody):
    if not verify_password(b.password):
        raise HTTPException(401, "密码错误")
    audit_log("admin", "登录", "登录成功")
    return {"token": _new_session(),
            "must_change": get_setting("password_changed") == "0"}


@app.post("/api/logout")
def logout(authorization: str = Header("")):
    _drop_session(authorization.replace("Bearer ", "").strip())
    return {"ok": True}


@app.post("/api/change-password")
def change_password(b: ChangePwBody, token: str = Depends(auth)):
    if not verify_password(b.old_password):
        raise HTTPException(400, "原密码错误")
    if len(b.new_password) < 4:
        raise HTTPException(400, "新密码至少 4 位")
    set_password(b.new_password)
    audit_log("admin", "修改密码", "管理密码已修改")
    return {"ok": True}


# ---------- 探针大屏 ----------
def _fmt_hw():
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        try:
            load = list(os.getloadavg())
        except Exception:
            load = [0, 0, 0]
        net = psutil.net_io_counters()
        return {
            "cpu_percent": round(cpu, 1),
            "mem_percent": round(mem.percent, 1),
            "mem_used_gb": round(mem.used / 1024**3, 2),
            "mem_total_gb": round(mem.total / 1024**3, 2),
            "disk_percent": round(disk.percent, 1),
            "disk_used_gb": round(disk.used / 1024**3, 1),
            "disk_total_gb": round(disk.total / 1024**3, 1),
            "load": [round(x, 2) for x in load],
            "net_up": net.bytes_sent,
            "net_down": net.bytes_recv,
        }
    except Exception:
        return {}


@app.get("/api/dashboard")
def dashboard(token: str = Depends(auth)):
    snap = stats_mod.snapshot()
    totals = stats_mod.inbound_traffic_totals()
    ibs = ib_mod.list_inbounds()
    running = proc_mod.is_running()

    inbound_rows = []
    total_up = total_down = 0
    for ib in ibs:
        t = totals.get(ib["id"], {"up": 0, "down": 0})
        total_up += t["up"]
        total_down += t["down"]
        rate = snap["inbound_rates"].get(ib["tag"], {"up": 0, "down": 0})
        inbound_rows.append({
            "id": ib["id"], "tag": ib["tag"], "protocol": ib["protocol"],
            "port": ib["port"], "enabled": bool(ib["enabled"]),
            "online": bool(ib["enabled"]) and running,
            "up": t["up"], "down": t["down"],
            "up_rate": round(rate["up"], 1), "down_rate": round(rate["down"], 1),
            "conn_count": snap["inbound_conns"].get(ib["tag"], 0),
        })

    return {
        "singbox": {
            "running": running,
            "installed": bool(sb_mod.effective_binary()),
            "version": sb_mod.detect_version(),
            "uptime": proc_mod.uptime(),
            "inbound_count": len(ibs),
            "config_path": sb_mod.effective_config_path(),
            "service": sb_mod.effective_service(),
        },
        "speed": {
            "up": round(snap["current"]["up"], 1),
            "down": round(snap["current"]["down"], 1),
            "up_peak": round(snap["peak"]["up"], 1),
            "down_peak": round(snap["peak"]["down"], 1),
            "history": [{"t": int(ts), "up": round(u, 1), "down": round(d, 1)}
                        for ts, u, d in snap["history"]],
        },
        "traffic": {"total_up": total_up, "total_down": total_down,
                    "by_inbound": inbound_rows},
        "hardware": _fmt_hw(),
    }


@app.post("/api/dashboard/reset-peak")
def reset_peak(token: str = Depends(auth)):
    stats_mod.reset_peak()
    return {"ok": True}


# ---------- 入站 ----------
class InboundBody(BaseModel):
    protocol: str
    port: int
    tag: str = ""
    settings: dict = {}


@app.get("/api/inbounds")
def api_list_inbounds(token: str = Depends(auth)):
    return ib_mod.list_inbounds()


@app.post("/api/inbounds")
def api_create_inbound(b: InboundBody, token: str = Depends(auth)):
    try:
        ib = ib_mod.create_inbound(b.protocol, b.port, b.tag, b.settings)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ok, msg = proc_mod.reload_config()
    if not ok:
        ib_mod.delete_inbound(ib["id"])
        raise HTTPException(400, f"配置校验失败已回滚: {msg}")
    audit_log("admin", "新增入站", f"{ib['tag']} ({ib['protocol']}:{ib['port']})")
    return ib


@app.put("/api/inbounds/{ib_id}")
def api_update_inbound(ib_id: str, b: dict, token: str = Depends(auth)):
    try:
        ib = ib_mod.update_inbound(ib_id, **b)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ok, msg = proc_mod.reload_config()
    if not ok:
        raise HTTPException(400, f"配置校验失败: {msg}")
    audit_log("admin", "修改入站", f"{ib['tag']} ({ib_id})")
    return ib


@app.delete("/api/inbounds/{ib_id}")
def api_delete_inbound(ib_id: str, token: str = Depends(auth)):
    ib = ib_mod.get_inbound(ib_id)
    ib_mod.delete_inbound(ib_id)
    proc_mod.reload_config()
    audit_log("admin", "删除入站", f"{ib['tag'] if ib else ib_id}")
    return {"ok": True}


@app.post("/api/inbounds/{ib_id}/regen-credential")
def api_regen_cred(ib_id: str, token: str = Depends(auth)):
    try:
        ib = ib_mod.regen_credential(ib_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    proc_mod.reload_config()
    audit_log("admin", "重新生成凭证", f"{ib['tag']}")
    return ib


class BackupBody(BaseModel):
    credential: str = ""


@app.post("/api/inbounds/{ib_id}/backups")
def api_add_backup(ib_id: str, b: BackupBody, token: str = Depends(auth)):
    try:
        ib = ib_mod.add_backup(ib_id, b.credential)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ok, msg = proc_mod.reload_config()
    if not ok:
        raise HTTPException(400, f"配置校验失败: {msg}")
    audit_log("admin", "添加备用凭证", f"{ib['tag']}")
    return ib


@app.delete("/api/inbounds/{ib_id}/backups")
def api_del_backup(ib_id: str, credential: str, token: str = Depends(auth)):
    try:
        ib = ib_mod.del_backup(ib_id, credential)
    except ValueError as e:
        raise HTTPException(400, str(e))
    proc_mod.reload_config()
    audit_log("admin", "删除备用凭证", f"{ib['tag']}")
    return ib


# ---------- 订阅 ----------
@app.get("/api/inbounds/{ib_id}/link")
def api_inbound_link(ib_id: str, request: Request, token: str = Depends(auth)):
    host = get_setting("public_host") or request.client.host
    try:
        return sub_mod.inbound_link(ib_id, host)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/sub")
def sub_aggregate(request: Request):
    host = get_setting("public_host") or (request.client.host if request.client else "")
    links = sub_mod.aggregate_links(host)
    if not links:
        raise HTTPException(404, "暂无可用节点")
    return PlainTextResponse("\n".join(links),
                             headers={"Content-Disposition": "attachment; filename=sub.txt"})


# ---------- 进程控制 ----------
@app.post("/api/singbox/start")
def api_start(token: str = Depends(auth)):
    ok, msg = proc_mod.start()
    audit_log("admin", "启动 sing-box", msg)
    return {"ok": ok, "msg": msg}


@app.post("/api/singbox/stop")
def api_stop(token: str = Depends(auth)):
    ok, msg = proc_mod.stop()
    audit_log("admin", "停止 sing-box", msg)
    return {"ok": ok, "msg": msg}


@app.post("/api/singbox/restart")
def api_restart(token: str = Depends(auth)):
    ok, msg = proc_mod.restart()
    audit_log("admin", "重启 sing-box", msg)
    return {"ok": ok, "msg": msg}


@app.get("/api/singbox/logs")
def api_logs(lines: int = 100, token: str = Depends(auth)):
    return {"logs": proc_mod.tail_logs(min(lines, 500))}


@app.post("/api/singbox/check")
def api_check(token: str = Depends(auth)):
    ok, msg = config_mgr.check_config()
    return {"ok": ok, "msg": msg}


@app.get("/api/singbox/config")
def api_config(token: str = Depends(auth)):
    return config_mgr.read_config()


# ---------- 设置 ----------
class SettingsBody(BaseModel):
    public_host: str = ""
    sb_binary: str = ""
    sb_config: str = ""
    sb_service: str = ""


@app.get("/api/settings")
def api_get_settings(token: str = Depends(auth)):
    return {"public_host": get_setting("public_host"),
            "sb_binary": get_setting("sb_binary"),
            "sb_config": get_setting("sb_config") or sb_mod.DEFAULT_CONFIG_PATH,
            "sb_service": get_setting("sb_service") or sb_mod.DEFAULT_SERVICE,
            "takeover_done": get_setting("takeover_done")}


@app.post("/api/settings")
def api_set_settings(b: SettingsBody, token: str = Depends(auth)):
    if b.public_host is not None:
        set_setting("public_host", b.public_host.strip())
    if b.sb_binary is not None:
        set_setting("sb_binary", b.sb_binary.strip())
    if b.sb_config:
        set_setting("sb_config", b.sb_config.strip())
    if b.sb_service:
        set_setting("sb_service", b.sb_service.strip())
    audit_log("admin", "修改设置", f"public_host={b.public_host} sb_config={b.sb_config} sb_service={b.sb_service}")
    return {"ok": True}


# ---------- 外部接管 ----------
@app.get("/api/singbox/detect")
def api_detect(token: str = Depends(auth)):
    return sb_mod.detect_installation()


@app.post("/api/singbox/takeover")
def api_takeover(token: str = Depends(auth)):
    try:
        r = takeover_mod.import_existing()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"接管失败: {e}")
    audit_log("admin", "接管现有配置",
              f"备份 {r['backup'] or '无'}；导入 {len(r['imported'])} 个，跳过 {len(r['skipped'])} 个")
    return r


# ---------- 监控增强：历史流量 / 连接 / 审计 ----------
@app.get("/api/traffic/history")
def api_traffic_history(rng: str = Query("24h", alias="range"), token: str = Depends(auth)):
    """?range=24h -> 10分钟粒度曲线；?range=30d -> 按天柱状（FastAPI 用别名接收 range 参数）"""
    import time as _t
    now = int(_t.time())
    if rng == "30d":
        since = now - 30 * 86400
        bucket = 86400
    else:
        rng = "24h"
        since = now - 24 * 3600
        bucket = 600
    rows = traffic_history_range(since - bucket)
    ibs = {ib["id"]: ib["tag"] for ib in ib_mod.list_inbounds()}
    ibs["__total__"] = "总计"

    buckets = {}
    for r in rows:
        b = r["ts"] // bucket * bucket
        key = (b, r["inbound_id"])
        d = buckets.setdefault(key, [0, 0])
        d[0] += r["up_bytes"]
        d[1] += r["down_bytes"]
    ts_list = sorted({b for b, _ in buckets})
    # 24h 补全空桶，30d 按天补全
    if rng == "24h":
        start = since // bucket * bucket
        ts_list = [start + i * bucket for i in range(int((now - start) // bucket) + 1)]
    else:
        import datetime as _dt
        days = []
        d0 = _dt.datetime.fromtimestamp(since).date()
        for i in range(31):
            dd = d0 + _dt.timedelta(days=i)
            if _dt.datetime(dd.year, dd.month, dd.day).timestamp() > now:
                break
            days.append(int(_dt.datetime(dd.year, dd.month, dd.day).timestamp()))
        ts_list = days

    def series(ib_id):
        return [buckets.get((t, ib_id), [0, 0]) for t in ts_list]

    total = series("__total__")
    per_ib = {ibs[i]: {"up": [x[0] for x in series(i)], "down": [x[1] for x in series(i)]}
              for i in ibs if i != "__total__" and any(x[0] or x[1] for x in series(i))}
    return {
        "range": rng, "bucket": bucket,
        "labels": ts_list,
        "total_up": [x[0] for x in total],
        "total_down": [x[1] for x in total],
        "by_inbound": per_ib,
    }


@app.get("/api/connections")
def api_connections(token: str = Depends(auth)):
    return stats_mod.list_connections()


@app.delete("/api/connections/{conn_id}")
def api_close_connection(conn_id: str, token: str = Depends(auth)):
    ok = stats_mod.close_connection(conn_id)
    if ok:
        audit_log("admin", "关闭连接", f"连接 {conn_id}")
    return {"ok": ok}


@app.get("/api/audit")
def api_audit(limit: int = 100, token: str = Depends(auth)):
    return list_audit(min(limit, 500))


# ---------- 前端托管 ----------
@app.get("/")
def index():
    p = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.isfile(p):
        return FileResponse(p)
    return {"msg": "sing-box 面板 API 运行中"}


init_db()
stats_mod.start_poller()
