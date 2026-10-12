"""Persistent research service: owns the worker process, watchdog, crash/backoff policy, control state.

Run:  python -m strategylab.service      (the dashboard launches it automatically; no CLI needed in normal use)
The service stays alive when research is Stopped (idle) so Start from the dashboard works; it exits only on SIGTERM/Ctrl-C.
"""
from __future__ import annotations
import os, signal, subprocess, sys, time
from pathlib import Path
from .config import Settings, home_dir
from .db import DB
from . import resources

WORKER_CMD = [sys.executable, "-m", "strategylab.worker"]


class Service:
    def __init__(self):
        self.db = DB(); self.st = Settings.load(); self.procs = {}; self.started_at = {}; self.next_at = {}; self.crashes = []; self.running = True
        self.lock = home_dir() / "service.pid"; self.next_start = 0.0; self.worker_started_at = 0.0

    def acquire(self):
        if self.lock.exists():
            try:
                pid = int(self.lock.read_text())
                # a live pid number alone is not proof: after a container restart the old pid can be reused or invisible. Require a fresh heartbeat too.
                fresh = time.time() - float(self.db.get("service_heartbeat", 0) or 0) < 90
                if pid != os.getpid() and resources.pid_alive(pid) and fresh: print("service already running", pid); sys.exit(0)
            except ValueError: pass
        self.lock.write_text(str(os.getpid()))

    def n_workers(self):
        return max(1, int(Settings.load().n_workers))

    def start_worker(self, i=0):
        env = dict(os.environ); env["STRATEGYLAB_WORKER"] = str(i)
        self.started_at[i] = time.time(); self.worker_started_at = max(self.worker_started_at, self.started_at[i])
        self.procs[i] = subprocess.Popen(WORKER_CMD, env=env, cwd=str(Path(__file__).resolve().parent.parent))
        self.db.log("info", "service", f"worker {i} started pid={self.procs[i].pid}")

    def kill_worker(self, reason, i=None):
        for j in ([i] if i is not None else list(self.procs)):
            p = self.procs.get(j)
            if p and p.poll() is None:
                self.db.log("warn", "service", f"terminating worker {j}: {reason}")
                p.terminate()
                try: p.wait(10)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
            self.procs.pop(j, None)

    @property
    def proc(self):            # primary worker handle (kept for callers/tests)
        return self.procs.get(0)

    def progress_stalled(self, now=None):
        """True when jobs are queued/runnable but none has COMPLETED for progress_stall_s (a heartbeat alone is not progress)."""
        now = now or time.time(); db = self.db
        last = db.q("SELECT COALESCE(MAX(finished),0) m FROM jobs WHERE status='done'")[0]["m"]
        runnable = db.q("SELECT COUNT(*) n FROM jobs WHERE status='queued' AND not_before<=?", (now,))[0]["n"]
        started = max(self.worker_started_at, last)
        return runnable > 0 and now - started > self.st.progress_stall_s and now - last > self.st.progress_stall_s

    def tick(self):
        db, st = self.db, self.st
        db.set("service_heartbeat", time.time()); db.set("service_pid", os.getpid()); n = self.n_workers(); db.set("n_workers", n)
        ctl = db.get("control", "stopped"); now = time.time()
        for i in list(self.procs):                           # reap exited workers
            p = self.procs[i]
            if p.poll() is None: continue
            rc = p.returncode; self.procs.pop(i)
            if ctl == "running" and rc != 0:
                self.crashes.append(now); self.crashes = [t for t in self.crashes if t > now - 600]
                k = db.reclaim_all_running(worker=i)
                delay = min(2 ** len(self.crashes), 300)
                self.next_at[i] = now + (0 if rc == 75 else delay)
                db.log("error", "service", f"worker {i} exited rc={rc}; {k} job(s) requeued; restart in {self.next_at[i] - now:.0f}s (crashes/10min={len(self.crashes)})")
                if len(self.crashes) >= 6 * n:
                    db.set("blocker", "crash_loop: waiting 5 min before retry (bounded, will retry automatically)")
                    for j in range(n): self.next_at[j] = now + 300
                    self.crashes = []
        if ctl == "running":
            for i in range(n):
                if i not in self.procs and now >= self.next_at.get(i, 0):
                    db.reclaim_all_running(worker=i); db.set(db.wkey("current_job", i), None); self.start_worker(i)
            for i in [j for j in self.procs if j >= n]: self.kill_worker("scaled down", i)
            for i in list(self.procs):
                hb = db.get(db.wkey("worker_heartbeat", i), now)
                if now - hb > st.watchdog_stall_s and now - self.started_at.get(i, 0) > st.watchdog_stall_s:
                    self.kill_worker("stalled (no heartbeat)", i); self.crashes.append(now); db.reclaim_all_running(worker=i)
                cj = db.get(db.wkey("current_job", i))
                if cj and now - cj["since"] > st.job_timeout_s and db.q("SELECT 1 FROM jobs WHERE id=? AND status='running' AND worker=?", (cj["id"], i)):      # a stale marker left by a previous process (container sleep/restart) is not a hung job
                    db.log("error", "service", f"job {cj['id']} exceeded timeout {st.job_timeout_s}s"); self.kill_worker("job timeout", i)
                    db.reclaim_all_running(worker=i)
            if self.procs and self.progress_stalled():
                db.log("error", "service", f"no job completed for {st.progress_stall_s}s despite queued work; restarting workers")
                self.kill_worker("progress stall"); self.crashes.append(now); db.reclaim_all_running(); return
            k = db.reclaim_stalled(st.watchdog_stall_s)
            if k: db.log("warn", "service", f"reclaimed {k} stalled job(s)")
        elif ctl == "paused":
            pass   # workers idle themselves
        else:  # stopped
            if self.procs and now - db.get("stop_requested", 0) > 20: self.kill_worker("user stop")

    def run(self):
        self.acquire()
        self.db.log("info", "service", f"service started pid={os.getpid()}")
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "running", False))
        signal.signal(signal.SIGINT, lambda *a: setattr(self, "running", False))
        reclaimed = self.db.reclaim_all_running()
        if reclaimed: self.db.log("info", "service", f"recovered {reclaimed} interrupted job(s) after restart")
        while self.running:
            try: self.tick()
            except Exception as e: self.db.log("error", "service", f"tick error {e}")
            time.sleep(1)
        self.kill_worker("service shutdown"); self.db.log("info", "service", "service stopped gracefully")
        try: self.lock.unlink()
        except Exception: pass


def main(): Service().run()


if __name__ == "__main__":
    main()
