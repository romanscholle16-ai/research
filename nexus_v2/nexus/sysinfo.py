"""Dependency-free system metrics (Linux /proc, Windows ctypes). GPU reported via nvidia-smi if present."""
import os, shutil, subprocess, sys, time


def mem() -> dict:
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = MS(); s.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return {"total_mb": s.ullTotalPhys // 2**20, "avail_mb": s.ullAvailPhys // 2**20, "used_pct": float(s.dwMemoryLoad)}
        d = {}
        for ln in open("/proc/meminfo"):
            k, v = ln.split(":")
            d[k] = int(v.split()[0]) // 1024
        t, a = d["MemTotal"], d.get("MemAvailable", d["MemFree"])
        return {"total_mb": t, "avail_mb": a, "used_pct": round(100 * (t - a) / t, 1)}
    except Exception:
        return {"total_mb": 0, "avail_mb": 0, "used_pct": 0.0}


_cpu_prev = {"idle": None, "tot": None}


def _win_cpu() -> float:
    """System CPU utilisation since the previous call (GetSystemTimes)."""
    import ctypes
    from ctypes import wintypes
    i, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
    f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
    idle, tot = f(i), f(k) + f(u)               # kernel time includes idle time
    pi, pt = _cpu_prev["idle"], _cpu_prev["tot"]
    _cpu_prev.update(idle=idle, tot=tot)
    if pi is None or tot == pt:
        return 0.0
    return round(100.0 * (1 - (idle - pi) / (tot - pt)), 1)


def cpu_load() -> float:
    try:
        if sys.platform.startswith("win"):
            return _win_cpu()
        return round(os.getloadavg()[0] / (os.cpu_count() or 1) * 100, 1)
    except (AttributeError, OSError):
        return 0.0


def disk(path) -> dict:
    u = shutil.disk_usage(str(path))
    return {"free_gb": round(u.free / 2**30, 2), "total_gb": round(u.total / 2**30, 1), "used_pct": round(100 * u.used / u.total, 1)}


_gpu = {"t": 0, "v": None}


def gpu() -> dict:
    """VRAM is detected, never assumed. Research code does not require a GPU."""
    if time.time() - _gpu["t"] < 60 and _gpu["v"] is not None:
        return _gpu["v"]
    res = {"present": False}
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout.strip()
            n, tot, used, util = [x.strip() for x in out.splitlines()[0].split(",")]
            res = {"present": True, "name": n, "vram_total_mb": int(tot), "vram_used_mb": int(used), "util_pct": int(util)}
        except Exception:
            pass
    _gpu.update(t=time.time(), v=res)
    return res


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        if sys.platform.startswith("win"):
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259
        os.kill(int(pid), 0)
        # zombie check
        try:
            with open(f"/proc/{int(pid)}/stat") as f:
                return f.read().split(")")[-1].split()[0] != "Z"
        except OSError:
            return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def proc_rss_mb(pid) -> float:
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            h = ctypes.windll.kernel32.OpenProcess(0x1010, False, int(pid))     # QUERY_INFORMATION | VM_READ
            if not h:
                return 0.0
            c = PMC(); c.cb = ctypes.sizeof(PMC)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
            ctypes.windll.kernel32.CloseHandle(h)
            return c.WorkingSetSize / 2**20 if ok else 0.0
        except Exception:
            return 0.0
    try:
        with open(f"/proc/{int(pid)}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except Exception:
        return 0.0


def snapshot(data_path) -> dict:
    return {"mem": mem(), "cpu_pct": cpu_load(), "disk": disk(data_path), "gpu": gpu(), "cores": os.cpu_count()}
