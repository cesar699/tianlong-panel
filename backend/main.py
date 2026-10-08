"""sing-box 可视化管理面板 - FastAPI 入口（单用户轻量版）"""
import os
import secrets
import time

import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import config_mgr
import inbounds as ib_mod
import process as proc_mod
import singbox as sb_mod
import stats as stats_mod
import subscribe as sub_mod
from db import _conn, get_setting, init_db, set_password, set_setting, verify_password

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
            "version": sb_mod.detect_version() or get_setting("singbox_version"),
            "uptime": proc_mod.uptime(),
            "inbound_count": len(ibs),
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
    return ib


@app.delete("/api/inbounds/{ib_id}")
def api_delete_inbound(ib_id: str, token: str = Depends(auth)):
    ib_mod.delete_inbound(ib_id)
    proc_mod.reload_config()
    return {"ok": True}


@app.post("/api/inbounds/{ib_id}/regen-credential")
def api_regen_cred(ib_id: str, token: str = Depends(auth)):
    try:
        ib = ib_mod.regen_credential(ib_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    proc_mod.reload_config()
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
    return ib


@app.delete("/api/inbounds/{ib_id}/backups")
def api_del_backup(ib_id: str, credential: str, token: str = Depends(auth)):
    try:
        ib = ib_mod.del_backup(ib_id, credential)
    except ValueError as e:
        raise HTTPException(400, str(e))
    proc_mod.reload_config()
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
    return {"ok": ok, "msg": msg}


@app.post("/api/singbox/stop")
def api_stop(token: str = Depends(auth)):
    ok, msg = proc_mod.stop()
    return {"ok": ok, "msg": msg}


@app.post("/api/singbox/restart")
def api_restart(token: str = Depends(auth)):
    ok, msg = proc_mod.restart()
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
    singbox_version: str = ""


@app.get("/api/settings")
def api_get_settings(token: str = Depends(auth)):
    return {"public_host": get_setting("public_host"),
            "singbox_version": get_setting("singbox_version")}


@app.post("/api/settings")
def api_set_settings(b: SettingsBody, token: str = Depends(auth)):
    if b.public_host is not None:
        set_setting("public_host", b.public_host.strip())
    if b.singbox_version:
        set_setting("singbox_version", b.singbox_version.strip().lstrip("v"))
    return {"ok": True}


@app.post("/api/singbox/download")
def api_download(token: str = Depends(auth)):
    try:
        v = sb_mod.download()
    except Exception as e:
        raise HTTPException(400, f"下载失败: {e}")
    return {"ok": True, "version": v}


# ---------- 前端托管 ----------
@app.get("/")
def index():
    p = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.isfile(p):
        return FileResponse(p)
    return {"msg": "sing-box 面板 API 运行中"}


init_db()
stats_mod.start_poller()
