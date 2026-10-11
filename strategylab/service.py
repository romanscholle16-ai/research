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
        self.db = DB(); self.st = Settings.load(); self.proc = None; self.crashes = []; self.running = True
        self.lock = home_dir() / "service.pid"; self.next_start = 0.0

    def acquire(self):
        if self.lock.exists():
            try:
                pid = int(self.lock.read_text())
                if pid != os.getpid() and resources.pid_alive(pid): print("service already running", pid); sys.exit(0)
            except ValueError: pass
        self.lock.write_text(str(os.getpid()))

    def start_worker(self):
        env = dict(os.environ)
        self.proc = subprocess.Popen(WORKER_CMD, env=env, cwd=str(Path(__file__).resolve().parent.parent))
        self.db.log("info", "service", f"worker started pid={self.proc.pid}")

    def kill_worker(self, reason):
        if self.proc and self.proc.poll() is None:
            self.db.log("warn", "service", f"terminating worker: {reason}")
            self.proc.terminate()
            try: self.proc.wait(10)
            except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait()
        self.proc = None

    def tick(self):
        db, st = self.db, self.st
        db.set("service_heartbeat", time.time()); db.set("service_pid", os.getpid())
        ctl = db.get("control", "stopped")
        alive = self.proc is not None and self.proc.poll() is None
        if self.proc is not None and not alive:
            rc = self.proc.returncode; self.proc = None
            if ctl == "running" and rc != 0:
                self.crashes.append(time.time()); self.crashes = [t for t in self.crashes if t > time.time() - 600]
                n = db.reclaim_all_running()
                delay = min(2 ** len(self.crashes), 300)
                self.next_start = time.time() + (0 if rc == 75 else delay)
                db.log("error", "service", f"worker exited rc={rc}; {n} job(s) requeued; restart in {self.next_start - time.time():.0f}s (crashes/10min={len(self.crashes)})")
                if len(self.crashes) >= 6:
                    db.set("blocker", "crash_loop: waiting 5 min before retry (bounded, will retry automatically)")
                    self.next_start = time.time() + 300; self.crashes = []
        if ctl == "running":
            if not alive and time.time() >= self.next_start:
                db.reclaim_all_running(); self.start_worker()
            elif alive:
                hb = db.get("worker_heartbeat", time.time())
                if time.time() - hb > st.watchdog_stall_s:
                    self.kill_worker("stalled (no heartbeat)"); self.crashes.append(time.time()); db.reclaim_all_running()
                n = db.reclaim_stalled(st.watchdog_stall_s)
                if n: db.log("warn", "service", f"reclaimed {n} stalled job(s)")
                cj = db.get("current_job")
                if cj and time.time() - cj["since"] > st.job_timeout_s:
                    db.log("error", "service", f"job {cj['id']} exceeded timeout {st.job_timeout_s}s"); self.kill_worker("job timeout")
                    db.x("UPDATE jobs SET attempts=attempts WHERE id=?", (cj["id"],)); db.reclaim_all_running()
        elif ctl == "paused":
            pass   # worker idles itself
        else:  # stopped
            if alive: self.kill_worker("user stop") if time.time() - db.get("stop_requested", 0) > 20 else None

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
