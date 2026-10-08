"""sing-box 进程控制：start/stop/restart/status/logs"""
import os
import signal
import subprocess
import time

from db import DATA_DIR
from config_mgr import CONFIG_PATH, LOG_PATH, check_config, write_config
from singbox import bin_path, ensure_binary

PID_PATH = os.path.join(DATA_DIR, "sing-box.pid")


def _pid() -> int:
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return 0


def is_running() -> bool:
    return _pid() > 0


def uptime() -> int:
    pid = _pid()
    if not pid:
        return 0
    try:
        # /proc/pid 出生时间推算
        stat = os.stat(f"/proc/{pid}")
        return int(time.time() - stat.st_ctime)
    except Exception:
        return 0


def start() -> tuple:
    if is_running():
        return True, "sing-box 已在运行"
    ensure_binary()
    ok, msg = write_config()
    if not ok:
        return False, f"配置校验失败，拒绝启动: {msg}"
    os.makedirs(DATA_DIR, exist_ok=True)
    logf = open(LOG_PATH, "a")
    p = subprocess.Popen([bin_path(), "run", "-c", CONFIG_PATH],
                         stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True)
    with open(PID_PATH, "w") as f:
        f.write(str(p.pid))
    time.sleep(1)
    if is_running():
        return True, "sing-box 启动成功"
    return False, "启动失败，请查看日志"


def stop() -> tuple:
    pid = _pid()
    if not pid:
        return True, "sing-box 未运行"
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.25)
            if not is_running():
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.remove(PID_PATH)
    except OSError:
        pass
    return True, "sing-box 已停止"


def restart() -> tuple:
    stop()
    time.sleep(1)
    return start()


def reload_config() -> tuple:
    """重写配置并校验，通过则重启生效"""
    ok, msg = write_config()
    if not ok:
        return False, f"配置校验失败: {msg}"
    if is_running():
        return restart()
    return True, "配置已保存（sing-box 未运行）"


def tail_logs(lines: int = 100) -> str:
    if not os.path.isfile(LOG_PATH):
        return ""
    with open(LOG_PATH, "rb") as f:
        # 从尾部读取，避免大文件全量加载
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = 4096
        data = b""
        while len(data.split(b"\n")) <= lines + 1 and size > 0:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
            if size == 0:
                break
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.split("\n")[-lines:])
