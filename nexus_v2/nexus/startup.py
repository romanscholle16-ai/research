"""Windows startup registration via a visible per-user Scheduled Task (no elevation, no hidden persistence).
On non-Windows systems this only writes the script and says so - it never claims startup is configured."""
import os, pathlib, subprocess, sys
from . import config

TASK = "NEXUS_V2_Research"


def run(install: bool) -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    cmd = root / "scripts" / "start_nexus.cmd"
    if not sys.platform.startswith("win"):
        print(f"NOT CONFIGURED: automatic startup is only implemented for Windows (this host: {sys.platform}). "
              f"On Windows run `python -m nexus install-startup`. The launcher script is {cmd}.")
        return 0
    if install:
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/TR", f'"{cmd}"', "/SC", "ONLOGON", "/RL", "LIMITED", "/F"],
                           capture_output=True, text=True)
    else:
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True, text=True)
    print(r.stdout or r.stderr)
    if install and r.returncode == 0:
        q = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, text=True)
        print("verified registration:" if q.returncode == 0 else "REGISTRATION NOT VERIFIED", q.stdout[-300:])
        return q.returncode
    return r.returncode
