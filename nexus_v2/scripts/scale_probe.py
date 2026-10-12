"""Gate I helper: step agent_budget_per_hour through the safe API, measure, and stop at the first sign of trouble.
Usage: python scripts/scale_probe.py 150,300,600 150   (budgets per agent per hour; seconds per step)"""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nexus import config, db, memory, runtime, server, sysinfo

budgets = [int(x) for x in sys.argv[1].split(",")]
secs = int(sys.argv[2])
port = int(os.environ.get("NEXUS_PORT", "8765"))
out = []
c = db.connect()
for b in budgets:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", json.dumps({"agent_budget_per_hour": b}).encode(),
                                 {"Content-Type": "application/json", "X-Nexus-Token": server.token()})
    urllib.request.urlopen(req, timeout=10).read()
    k0 = memory.counters(c); t0 = time.time(); cpu = []; mem = []
    while time.time() - t0 < secs:
        cpu.append(sysinfo.cpu_load()); mem.append(sysinfo.mem()["used_pct"]); time.sleep(5)
    k1 = memory.counters(c); dt = time.time() - t0
    h = runtime.health(c)
    rss = {p["name"]: round(sysinfo.proc_rss_mb(p["pid"])) for p in h["processes"] if p["alive"]}
    row = {"budget_per_agent_per_hour": b, "experiments_per_min": round((k1["experiments_completed"] - k0["experiments_completed"]) / dt * 60, 1),
           "cpu_load_pct_avg": round(sum(cpu) / len(cpu), 1), "mem_pct_max": max(mem), "queue_pending": k1["queue_pending"], "health": h["status"],
           "new_incidents": k1["incidents_total"] - k0["incidents_total"], "quarantined": k1["quarantined"],
           "dup_rejections": k1["duplicate_rejections"] - k0["duplicate_rejections"], "worker_rss_mb": {k: v for k, v in rss.items() if k.startswith("worker")}}
    out.append(row); print(json.dumps(row), flush=True)
    if h["status"] not in ("healthy", "idle") or row["new_incidents"] or row["cpu_load_pct_avg"] > 70 or row["mem_pct_max"] > 75 or row["queue_pending"] > 150:
        print("STOP: trouble signals; not scaling further", flush=True); break
json.dump(out, open(config.path("scale_probe.json"), "w"), indent=1)
