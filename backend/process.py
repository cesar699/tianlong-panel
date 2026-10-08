"""sing-box 进程控制（外部接管模式）：全部走 systemctl / journalctl。

systemctl 与 journalctl 调用分别封装为 _systemctl() / _journalctl()，
便于单元测试 mock（沙箱无 systemd）。
"""
import subprocess
import time

from singbox import effective_service


def _run_cmd(argv: list) -> subprocess.CompletedProcess:
    """唯一外部命令执行点（mock 点：单测替换此函数验证命令拼装）"""
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def _systemctl(*args) -> subprocess.CompletedProcess:
    return _run_cmd(["systemctl", *args, effective_service()])


def _journalctl(lines: int) -> subprocess.CompletedProcess:
    return _run_cmd(["journalctl", "-u", effective_service(),
                     "-n", str(lines), "--no-pager"])


_run_cache = {"ts": 0, "val": False}
_CACHE_TTL = 5


def is_running() -> bool:
    now = time.time()
    if now - _run_cache["ts"] < _CACHE_TTL:
        return _run_cache["val"]
    try:
        val = _systemctl("is-active").stdout.strip() == "active"
    except Exception:
        val = False
    _run_cache.update(ts=now, val=val)
    return val


def uptime() -> int:
    """systemctl show 取单调时钟启动时间戳"""
    try:
        out = _systemctl("show", "-p", "ActiveEnterTimestampMonotonic", "--value").stdout.strip()
        us = int(out)
        if us > 0:
            return max(int(time.monotonic() - us / 1_000_000), 0)
    except Exception:
        pass
    return 0


def _do(action: str) -> tuple:
    try:
        p = _systemctl(action)
    except FileNotFoundError:
        return False, "系统无 systemctl（容器内请直接在宿主机操作，或裸机部署面板）"
    except Exception as e:
        return False, f"systemctl 调用失败: {e}"
    if p.returncode == 0:
        return True, f"systemctl {action} {effective_service()} 成功"
    err = (p.stderr or p.stdout).strip()[-300:]
    return False, f"systemctl {action} 失败: {err or '未知错误'}"


def start() -> tuple:
    if is_running():
        return True, "sing-box 已在运行"
    return _do("start")


def stop() -> tuple:
    if not is_running():
        return True, "sing-box 未运行"
    return _do("stop")


def restart() -> tuple:
    return _do("restart")


def reload_config() -> tuple:
    """重写配置并校验，通过则重启服务生效"""
    from config_mgr import write_config
    ok, msg = write_config()
    if not ok:
        return False, f"配置校验失败: {msg}"
    if is_running():
        return restart()
    return True, "配置已保存（服务未运行，启动后生效）"


def tail_logs(lines: int = 100) -> str:
    try:
        p = _journalctl(lines)
    except FileNotFoundError:
        return "系统无 journalctl（容器内请在宿主机执行 journalctl -u sing-box）"
    except Exception as e:
        return f"读取日志失败: {e}"
    if p.returncode != 0:
        return (p.stderr or p.stdout).strip()[-500:] or "无日志"
    return p.stdout.strip()[-20000:]
