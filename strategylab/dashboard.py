"""Dashboard (stdlib http.server, localhost only) + service launcher. Works offline from the DB (shows last-known progress)."""
from __future__ import annotations
import json, os, subprocess, sys, time, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .config import Settings, home_dir, LIVE_TRADING_ENABLED
from .db import DB
from .ai import Router
from . import resources

HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8") if (Path(__file__).parent / "dashboard.html").exists() else "dashboard.html missing"
RANK = {"BACKTEST_QUALIFIED": 0, "PAPER_VALIDATED": 0, "LIVE_ELIGIBLE": 0, "PROMISING": 1, "REJECTED": 2}


def service_alive(db):
    hb = db.get("service_heartbeat", 0); pid = db.get("service_pid")
    return bool(pid) and time.time() - hb < 15 and resources.pid_alive(pid)


def ensure_service(db):
    if service_alive(db): return True
    kw = {}
    if os.name == "nt": kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else: kw["start_new_session"] = True
    log = open(home_dir() / "service.log", "ab")
    subprocess.Popen([sys.executable, "-m", "strategylab.service"], stdout=log, stderr=log, cwd=str(Path(__file__).resolve().parent.parent), **kw)
    return True


def status(db):
    st = Settings.load(); cj = db.get("current_job")
    nxt = db.q("SELECT payload FROM jobs WHERE status='queued' AND kind='experiment' ORDER BY priority,id LIMIT 1")
    nxt_p = json.loads(nxt[0]["payload"]) if nxt else None
    real = db.counts(0); syn = db.counts(1)
    best = db.q("""SELECT strategy_id, symbol, timeframe, status, summary_json FROM strategy_status WHERE synthetic=0 AND status!='REJECTED' ORDER BY updated DESC LIMIT 50""")
    best_val = None
    for b in best:
        sm = json.loads(b["summary_json"]); t = sm.get("final", {}).get("test", {})
        if t and (best_val is None or t.get("mean_R", -9) > best_val["test_mean_R"]):
            best_val = dict(strategy_id=b["strategy_id"], symbol=b["symbol"], timeframe=b["timeframe"], test_mean_R=t["mean_R"], test_pf=t["pf"], n=t["n"])
    ds = db.q("SELECT symbol,timeframe,source,rows,start_ts,end_ts,gaps,has_funding,synthetic FROM datasets ORDER BY created DESC LIMIT 12")
    return dict(
        control=db.get("control", "stopped"), service_alive=service_alive(db), live_trading_enabled=LIVE_TRADING_ENABLED,
        worker_heartbeat_age=time.time() - db.get("worker_heartbeat", 0) if db.get("worker_heartbeat") else None,
        cycles=db.get("cycles", 0), current_job=cj, next_experiment=nxt_p, last_experiment=db.get("last_experiment"),
        blocker=db.get("blocker"), counts_real=real, counts_synthetic=syn, best_validated=best_val,
        queue=db.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status"),
        ai=dict(state=db.get("ai_state", "unknown"), **Router(db, st).status()),
        research_mode=Router(db, st).mode(), paid_apis_enabled=Router(db, st).paid_allowed(), exchange_preset=st.exchange_preset, timeframes=st.timeframes,
        events=db.q("SELECT ts,level,source,message FROM events WHERE level IN ('warn','error') ORDER BY id DESC LIMIT 15"),
        datasets=ds, rss_mb=resources.rss_mb(), free_mem_mb=resources.free_mem_mb(),
        now=time.time())



def leaderboard(db, synthetic=0, limit=40):
    rows = db.q("""SELECT ss.strategy_id sid, ss.symbol, ss.timeframe, ss.status, ss.summary_json sj, ss.synthetic syn, s.family, s.spec_json
                   FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id WHERE ss.synthetic=? ORDER BY ss.updated DESC LIMIT 4000""", (synthetic,))
    out = []
    for r in rows:
        sm = json.loads(r["sj"]); m = sm.get("final", {}).get("test") or sm.get("validate", {}).get("val") or sm.get("screen") or {}
        cap = (sm.get("final") or {}).get("capital_all") or {}
        out.append(dict(id=r["sid"], name=f'{r["family"]}', family=r["family"], symbol=r["symbol"], timeframe=r["timeframe"], status=r["status"],
                        trades=m.get("n", 0), mean_R=m.get("mean_R", 0), pf=m.get("pf", 0), max_dd=m.get("max_dd", 0), t=m.get("t", 0),
                        stage="final(test)" if sm.get("final") else "validate(val)" if sm.get("validate") else "screen(train)",
                        p_target=(cap.get("monthly_withdraw_any") or {}).get("1000", {}).get("p"), p_ruin=(cap.get("ruin") or {}).get("p"),
                        reasons=sm.get("reasons", []), synthetic=bool(r["syn"])))
    out.sort(key=lambda x: (RANK.get(x["status"], 3), {"final(test)": 0, "validate(val)": 1, "screen(train)": 2}[x["stage"]], -x["t"]))
    return out[:limit]


def capital(db, sid, symbol, tf):
    r = db.q("SELECT summary_json FROM strategy_status WHERE strategy_id=? AND symbol=? AND timeframe=?", (sid, symbol, tf))
    if not r: return {}
    sm = json.loads(r[0]["summary_json"]); f = sm.get("final") or {}
    return dict(sizing=f.get("sizing_fit_on_train_val"), capital_test=f.get("capital_test"), capital_all=f.get("capital_all"), multiple_testing=f.get("multiple_testing"),
                weekly_coverage_test=f.get("weekly_coverage_test"), reasons=sm.get("reasons"))


class H(BaseHTTPRequestHandler):
    db = None
    def log_message(self, *a): pass
    def _send(self, obj, code=200, ctype="application/json"):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj, default=float).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/": return self._send(HTML, ctype="text/html; charset=utf-8")
            if u.path == "/api/status": return self._send(status(self.db))
            if u.path == "/api/leaderboard": return self._send(leaderboard(self.db, int(q.get("synthetic", 0))))
            if u.path == "/api/capital": return self._send(capital(self.db, q["sid"], q["symbol"], q["tf"]))
            if u.path == "/api/settings": return self._send(Settings.load().__dict__)
            self._send({"error": "not found"}, 404)
        except Exception as e: self._send({"error": str(e)}, 500)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("Origin") and "127.0.0.1" not in self.headers["Origin"] and "localhost" not in self.headers["Origin"]:
            return self._send({"error": "forbidden origin"}, 403)
        db = self.db
        if self.path == "/api/control":
            a = body.get("action")
            if a in ("start", "resume"):
                ensure_service(db); db.set("control", "running"); db.set("blocker", None); db.log("info", "dashboard", f"user: {a}")
            elif a == "pause": db.set("control", "paused"); db.log("info", "dashboard", "user: pause")
            elif a == "stop": db.set("stop_requested", time.time()); db.set("control", "stopped"); db.log("info", "dashboard", "user: stop")
            else: return self._send({"error": "bad action"}, 400)
            return self._send({"ok": True, "control": db.get("control")})
        if self.path == "/api/settings":
            s = Settings.load(); s.update({k: v for k, v in body.items() if k in s.__dict__}); return self._send({"ok": True})
        self._send({"error": "not found"}, 404)


def serve(port=8765, open_browser=True):
    H.db = DB()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)   # localhost only
    if open_browser: threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"StrategyLab dashboard on http://127.0.0.1:{port}  (home={home_dir()})")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    serve(int(os.environ.get("STRATEGYLAB_PORT", 8765)), open_browser=not os.environ.get("STRATEGYLAB_NO_BROWSER"))
