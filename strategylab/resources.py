import os, sys
try:
    import psutil
except Exception:
    psutil = None


def rss_mb(pid=None) -> float:
    pid = pid or os.getpid()
    try:
        if psutil: return psutil.Process(pid).memory_info().rss / 1e6
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS"): return int(line.split()[1]) / 1024
    except Exception: pass
    return 0.0


def free_mem_mb() -> float:
    try:
        if psutil: return psutil.virtual_memory().available / 1e6
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"): return int(line.split()[1]) / 1024
    except Exception: pass
    return 1e9   # unknown => do not block


def pid_alive(pid) -> bool:
    try:
        if psutil: return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
        os.kill(pid, 0); return True
    except Exception: return False
