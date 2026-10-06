#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天龙面板 Agent
采集本机系统指标，定时上报到天龙面板服务端。
只依赖: psutil, requests

配置: 同目录 agent.json (见 agent.json.example)，
      或环境变量 TIANLONG_SERVER / TIANLONG_SECRET / TIANLONG_NAME / TIANLONG_INTERVAL
"""
import json
import os
import socket
import sys
import time
import platform

try:
    import psutil
    import requests
except ImportError as e:
    print(f"缺少依赖: {e.name}，请先 pip install psutil requests", file=sys.stderr)
    sys.exit(1)


def load_config():
    cfg = {}
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    return {
        "server":   os.environ.get("TIANLONG_SERVER", cfg.get("server", "http://127.0.0.1:8009")),
        "secret":   os.environ.get("TIANLONG_SECRET", cfg.get("secret", "")),
        "name":     os.environ.get("TIANLONG_NAME", cfg.get("name", socket.gethostname())),
        "interval": int(os.environ.get("TIANLONG_INTERVAL", cfg.get("interval", 10))),
    }


def collect(prev_net):
    """采集一轮指标，返回 (metrics dict, new_prev_net)"""
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    boot = psutil.boot_time()
    uptime = int(time.time() - boot)

    net = psutil.net_io_counters()
    if prev_net:
        dt = max(1, int(time.time() - prev_net["ts"]))
        net_in_bps = (net.bytes_recv - prev_net["recv"]) / dt
        net_out_bps = (net.bytes_sent - prev_net["sent"]) / dt
    else:
        net_in_bps = net_out_bps = 0
    new_prev = {"recv": net.bytes_recv, "sent": net.bytes_sent, "ts": time.time()}

    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            if info["cpu_percent"] is None:
                continue
            procs.append({
                "pid": info["pid"],
                "name": (info["name"] or "?")[:40],
                "cpu": round(info["cpu_percent"], 1),
                "mem": round(info["memory_percent"] or 0, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    top = procs[:8]

    return {
        "ts": int(time.time()),
        "cpu_pct": round(cpu, 1),
        "load1": round(load[0], 2),
        "mem_pct": round(mem.percent, 1),
        "mem_total_mb": round(mem.total / 1024 / 1024),
        "mem_used_mb": round(mem.used / 1024 / 1024),
        "disk_pct": round(disk.percent, 1),
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
        "net_in_bps": round(net_in_bps, 1),
        "net_out_bps": round(net_out_bps, 1),
        "uptime_s": uptime,
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "top_procs": top,
    }, new_prev


def report(cfg, metrics):
    url = cfg["server"].rstrip("/") + "/api/report"
    payload = {"name": cfg["name"], "secret": cfg["secret"], "metrics": metrics}
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def main():
    cfg = load_config()
    if not cfg["secret"]:
        print("未配置 secret，请在 agent.json 或环境变量 TIANLONG_SECRET 中设置", file=sys.stderr)
        sys.exit(1)
    print(f"天龙面板 Agent 启动，上报到 {cfg['server']}，主机名 {cfg['name']}，间隔 {cfg['interval']}s")
    # 预热 cpu_percent
    psutil.cpu_percent(interval=None)
    for p in psutil.process_iter(["cpu_percent"]):
        try:
            p.cpu_percent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    prev_net = None
    while True:
        try:
            metrics, prev_net = collect(prev_net)
            report(cfg, metrics)
            print(f"[{time.strftime('%H:%M:%S')}] 上报成功 "
                  f"CPU {metrics['cpu_pct']}% MEM {metrics['mem_pct']}% DISK {metrics['disk_pct']}%")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 上报失败: {e}", file=sys.stderr)
        time.sleep(cfg["interval"])


if __name__ == "__main__":
    main()
