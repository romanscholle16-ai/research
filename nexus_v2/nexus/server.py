"""Local dashboard server (127.0.0.1 only). The browser is never a source of truth: every number comes from
the database via these read endpoints; writes are limited to operational commands/settings."""
import json, os, pathlib, secrets, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from . import config, db, finsim, goals, memory, qualify, research as R, runtime, strategies as S, sysinfo, log, __version__, ENGINE_VERSION

LG = log.get("web")
UI = pathlib.Path(__file__).resolve().parents[1] / "ui"
PORT = int(os.environ.get("NEXUS_PORT", "8765"))
NAV = ["Dashboard", "Strategies", "Backtesting", "Research Agents", "Experiments", "Results", "Datasets", "Validation",
       "Financial Goals", "Research Memory", "Event Timeline", "Guardians", "System Health", "Logs", "Settings"]


def token():
    p = config.path("run", "web_token")
    if not p.exists():
        p.write_text(secrets.token_hex(16))
    return p.read_text().strip()


def rows(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a).fetchall()]


def agent_view(conn):
    from . import agents
    out = []
    st = db.settings(conn)
    fs = memory.family_stats(conn)
    for cls in agents.AGENT_CLASSES:
        fams = list(cls.families)
        n = conn.execute(f"SELECT COUNT(*) FROM experiments WHERE family IN ({','.join('?'*len(fams))})", fams).fetchone()[0]
        h = conn.execute("SELECT COUNT(*) FROM scheduler_log WHERE agent=? AND ts>? AND mode!='skip'", (cls.name, time.time() - 3600)).fetchone()[0]
        hy = rows(conn, "SELECT family,symbol,timeframe,status,n_experiments FROM hypotheses WHERE agent=? ORDER BY n_experiments DESC LIMIT 12", cls.name)
        last = rows(conn, "SELECT ts,family,mode,why,would_change FROM scheduler_log WHERE agent=? ORDER BY id DESC LIMIT 5", cls.name)
        out.append({"name": cls.name, "title": cls.title, "scope": cls.scope, "families": fams, "experiments": n, "budget_used_1h": h,
                    "budget_per_hour": st["agent_budget_per_hour"], "family_stats": {f: fs.get(f) for f in fams},
                    "promising": sum((fs.get(f) or {}).get("promising", 0) for f in fams), "hypotheses": hy, "recent_decisions": last})
    return out


def overview(conn):
    h = runtime.health(conn)
    k = memory.counters(conn)
    sid = goals.best_candidate(conn)
    gl = goals.compute(conn, sid)
    lb = goals.leaderboards(conn, 8)
    best_expl = lb["A"][0] if lb["A"] else None
    validated = conn.execute("SELECT c.strategy_id FROM candidates c WHERE c.status='qualified' LIMIT 1").fetchone()
    now = time.time()
    thr = rows(conn, "SELECT CAST((completed_at/3600) AS INT) h, COUNT(*) n FROM experiments WHERE completed_at>? GROUP BY h ORDER BY h", now - 24 * 3600)
    hist = [json.loads(r[0]) | {"ts": r[1]} for r in conn.execute("SELECT data_json,ts FROM metrics_hist WHERE ts>? ORDER BY ts", (now - 6 * 3600,))]
    ck = conn.execute("SELECT ts,integrity_ok FROM checkpoints ORDER BY id DESC LIMIT 1").fetchone()
    lr = db.get_state(conn, "last_recovery")
    data_row = conn.execute("SELECT symbol, MAX(end_ts) e, rows, gaps, dupes, bad_rows, data_class FROM datasets GROUP BY symbol").fetchall()
    return {"health": h, "counters": k, "goals": gl, "goals_candidate": sid, "leaderboards": lb, "best_exploratory": best_expl,
            "best_validated": dict(validated) if validated else None, "throughput": thr, "history": hist,
            "system": sysinfo.snapshot(config.home()), "agents": [{"name": a["name"], "title": a["title"], "experiments": a["experiments"],
                                                                 "budget_used_1h": a["budget_used_1h"], "promising": a["promising"]} for a in agent_view(conn)],
            "guardians": guardian_view(conn), "checkpoint": {"ts": ck[0], "ok": bool(ck[1])} if ck else None,
            "last_recovery": json.loads(lr) if lr else None, "incidents": rows(conn, "SELECT * FROM incidents ORDER BY id DESC LIMIT 8"),
            "hypotheses": rows(conn, "SELECT agent,family,symbol,timeframe,status,n_experiments,text FROM hypotheses ORDER BY updated_at DESC LIMIT 6"),
            "next_experiments": rows(conn, "SELECT ts,agent,family,mode,why FROM scheduler_log WHERE mode!='skip' ORDER BY id DESC LIMIT 6"),
            "data": [dict(r) for r in data_row], "activity": json.loads(db.get_state(conn, "activity", "{}") or "{}"),
            "settings": db.settings(conn), "version": __version__, "engine": ENGINE_VERSION, "live_trading": False}


def guardian_view(conn):
    out = []
    for n, nm in ((1, "guardian1_reliability"), (2, "guardian2_stagnation"), (3, "guardian3_integrity")):
        p = conn.execute("SELECT * FROM processes WHERE name=?", (f"guardian{n}",)).fetchone()
        last = rows(conn, "SELECT ts,check_name,ok,detail,action FROM guardian_log WHERE guardian=? ORDER BY id DESC LIMIT 12", nm)
        fails = conn.execute("SELECT COUNT(*) FROM guardian_log WHERE guardian=? AND ok=0 AND ts>?", (nm, time.time() - 3600)).fetchone()[0]
        out.append({"id": n, "name": nm, "alive": bool(p and sysinfo.pid_alive(p["pid"]) and p["status"] == "running"),
                    "heartbeat_age": round(time.time() - (p["heartbeat"] or 0), 1) if p else None, "failed_checks_1h": fails, "recent": last})
    return out


def strategy_detail(conn, sid):
    s = conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    if not s:
        return None
    ex = rows(conn, "SELECT id,stage,status,trades,net,expectancy_r,t_stat,pf,max_dd,failure_reason,lessons,next_suggestion,why_selected,completed_at FROM experiments WHERE strategy_id=? ORDER BY id", sid)
    cand = conn.execute("SELECT * FROM candidates WHERE strategy_id=?", (sid,)).fetchone()
    vals = rows(conn, "SELECT id,stage,passed,n_trades,metrics_json,thresholds_hash,engine_version,dataset_version,created_at FROM validations WHERE strategy_id=? ORDER BY id", sid)
    for v in vals:
        v["metrics"] = json.loads(v.pop("metrics_json"))
    assess = qualify.assess(conn, sid) if cand else None
    trades = rows(conn, "SELECT phase,exit_ts,net,r FROM candidate_trades WHERE strategy_id=? ORDER BY exit_ts", sid)
    eq, cur, peak, dd = [], config.CAPITAL["start_equity"], config.CAPITAL["start_equity"], 0
    for t in trades:
        cur += t["net"]; peak = max(peak, cur); dd = max(dd, (peak - cur) / peak)
        eq.append([t["exit_ts"], cur])
    st = goals.cand_stats(conn, sid) if cand else None
    stats = None
    if st:
        stats = {k: v for k, v in st.items() if k != "rows"}
    fees = conn.execute("SELECT COALESCE(SUM(fees),0),COALESCE(SUM(funding),0),COALESCE(SUM(slippage),0),COALESCE(SUM(net),0) FROM candidate_trades WHERE strategy_id=?", (sid,)).fetchone()
    return {"id": sid, "family": s["family"], "agent": s["agent"], "symbol": s["symbol"], "timeframe": s["timeframe"], "spec": json.loads(s["spec_json"]),
            "family_info": S.F[s["family"]]["info"], "experiments": ex, "candidate": dict(cand) if cand else None, "assessment": assess,
            "validations": vals, "goals": goals.compute(conn, sid) if cand else goals.compute(conn, None), "equity": eq[-400:], "max_dd_stored": dd,
            "stats": stats, "costs": {"fees": fees[0], "funding": fees[1], "slippage": fees[2], "net": fees[3]},
            "engine_version": ENGINE_VERSION, "thresholds_hash": config.trusted_hash(), "assumptions": config.COSTS}


def simulate_api(conn, body):
    sid = body.get("strategy_id")
    r = None
    if sid:
        r = [x["r"] for x in conn.execute("SELECT r FROM candidate_trades WHERE strategy_id=? AND (phase LIKE 'wf%' OR phase IN ('holdout','forward'))", (sid,))]
        if len(r) < 10:
            r = [x["r"] for x in conn.execute("SELECT r FROM candidate_trades WHERE strategy_id=?", (sid,))]
    hyp = {"win_rate": float(body.get("win_rate", 0.4)), "avg_win_r": float(body.get("avg_win_r", 2.0)), "avg_loss_r": float(body.get("avg_loss_r", 1.0))}
    return finsim.simulate(r_samples=r, hypothetical=hyp, risk_pct=float(body.get("risk_pct", config.RISK["risk_per_trade"])),
                           months=int(min(24, body.get("months", 6))), paths=int(min(5000, body.get("paths", 2000))),
                           trades_per_month=float(body.get("trades_per_month", 20)), cost_drag_r=float(body.get("cost_drag_r", 0.0)),
                           monthly_withdrawal_target=float(body.get("withdrawal", 0) or 0) or None, deposits_per_month=float(body.get("deposits", 0)))


def adhoc_backtest(conn, body):
    """Ad-hoc run restricted to the exploration region. NOT recorded as an experiment and never reaches the holdout."""
    sp = S.make_spec(body["family"], body.get("symbol", "ETHUSDT"), body.get("timeframe", "1h"), body.get("params", {}))
    ns = S.normalize(sp)
    m, split = R.get_market(conn, ns)
    res, mt, pm = R.eval_region(ns, m, 0, split["explore_end"])
    return {"spec": ns, "canonical_id": S.canonical_id(ns), "region": "exploration only (holdout inaccessible)", "recorded": False, "metrics": R.jd(mt),
            "equity": R.jd(res.equity_close[::max(1, len(res.equity_close) // 300)].tolist()),
            "note": "Ad-hoc runs are not experiments and do not count toward results; repeated tuning here is selection bias."}


class H(BaseHTTPRequestHandler):
    server_version = "nexus"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else (json.dumps(R.jd(body), default=str).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                html = (UI / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token()).replace("__NAV__", json.dumps(NAV))
                return self._send(200, html, "text/html")
            conn = db.connect()
            try:
                return self._api_get(conn, u.path, q)
            finally:
                conn.close()
        except Exception as e:
            LG.exception("GET %s", self.path)
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _api_get(self, conn, path, q):
        g = lambda k, d=None: q.get(k, [d])[0]
        lim = min(int(g("limit", 100)), 500)
        if path == "/api/health":
            return self._send(200, runtime.health(conn))
        if path == "/api/overview":
            return self._send(200, overview(conn))
        if path == "/api/goals":
            sid = g("strategy_id") or goals.best_candidate(conn)
            return self._send(200, {"strategy_id": sid, "goals": goals.compute(conn, sid), "capital": config.CAPITAL, "thresholds": config.THRESHOLDS})
        if path == "/api/strategies":
            return self._send(200, {"leaderboards": goals.leaderboards(conn, 25), "recent": rows(conn,
                "SELECT s.id,s.family,s.agent,s.symbol,s.timeframe,e.status,e.stage,e.trades,e.expectancy_r,e.t_stat FROM strategies s JOIN experiments e ON e.strategy_id=s.id ORDER BY e.id DESC LIMIT ?", lim),
                "families": {f: {"agent": v["agent"], "grid": S.grid_size(f), "title": v["info"]["title"]} for f, v in S.F.items()}})
        if path.startswith("/api/strategy/"):
            d = strategy_detail(conn, path.rsplit("/", 1)[1])
            return self._send(200 if d else 404, d or {"error": "not found"})
        if path == "/api/experiments":
            cond, a = ["1=1"], []
            for k in ("status", "family", "agent", "stage"):
                if g(k):
                    cond.append(f"{k}=?"); a.append(g(k))
            return self._send(200, {"rows": rows(conn, f"SELECT id,strategy_id,agent,family,stage,status,failure_reason,trades,net,expectancy_r,t_stat,pf,max_dd,runtime_ms,completed_at,lessons,next_suggestion,why_selected "
                                                       f"FROM experiments WHERE {' AND '.join(cond)} ORDER BY id DESC LIMIT ?", *a, lim),
                                    "by_status": rows(conn, "SELECT status,COUNT(*) n FROM experiments GROUP BY status")})
        if path == "/api/agents":
            return self._send(200, agent_view(conn))
        if path == "/api/guardians":
            return self._send(200, {"guardians": guardian_view(conn), "stagnation": rows(conn, "SELECT * FROM stagnation_events ORDER BY id DESC LIMIT 15"),
                                    "weights": rows(conn, "SELECT * FROM family_weights ORDER BY updated_at DESC")})
        if path == "/api/datasets":
            return self._send(200, {"datasets": rows(conn, "SELECT id,symbol,timeframe,source,retrieved_at,rows,start_ts,end_ts,gaps,dupes,bad_rows,version,data_class,manifest_json FROM datasets ORDER BY id DESC LIMIT 20"),
                                    "splits": rows(conn, "SELECT * FROM data_splits"), "files": conn.execute("SELECT COUNT(*),SUM(verified) FROM data_files").fetchone()[:],
                                    "exchange_specs": rows(conn, "SELECT symbol,field,value,status,source,note FROM exchange_specs WHERE symbol='ETHUSDT'")})
        if path == "/api/validation":
            return self._send(200, {"candidates": rows(conn, "SELECT c.*, s.family, s.symbol, s.timeframe FROM candidates c JOIN strategies s ON s.id=c.strategy_id ORDER BY c.dev_t DESC LIMIT ?", lim),
                                    "validations": rows(conn, "SELECT id,strategy_id,stage,passed,n_trades,created_at FROM validations ORDER BY id DESC LIMIT ?", lim),
                                    "holdout": rows(conn, "SELECT * FROM holdout_access ORDER BY id DESC LIMIT 50"), "thresholds": config.THRESHOLDS,
                                    "holdout_budget": {"used": conn.execute("SELECT COUNT(*) FROM holdout_access WHERE passed>=0").fetchone()[0], "max": config.THRESHOLDS["ho_max_evaluations_total"]}})
        if path == "/api/results":
            return self._send(200, {"counters": memory.counters(conn), "by_family": rows(conn, "SELECT family,COUNT(*) n,SUM(status='promising') prom,SUM(status='rejected') rej,SUM(status='inconclusive') inc,"
                                                                                       "AVG(expectancy_r) avg_exp,MAX(CASE WHEN stage='S2' THEN expectancy_r END) best_exp FROM experiments GROUP BY family ORDER BY n DESC"),
                                    "null_calibration": rows(conn, "SELECT family, COUNT(*) n, SUM(stage='S2') reached_s2, SUM(failure_reason LIKE 'null model%') passed_s2 FROM experiments WHERE family IN ('random_entry','periodic_hold') GROUP BY family"),
                                    "by_agent": rows(conn, "SELECT agent,COUNT(*) n,SUM(status IN ('promising','validation_failed','qualified')) prom FROM experiments GROUP BY agent")})
        if path == "/api/memory":
            return self._send(200, {"hypotheses": rows(conn, "SELECT * FROM hypotheses ORDER BY n_experiments DESC LIMIT ?", lim),
                                    "lessons": rows(conn, "SELECT id,family,status,lessons,next_suggestion,why_selected FROM experiments WHERE lessons!='' ORDER BY id DESC LIMIT 40"),
                                    "scheduler": rows(conn, "SELECT * FROM scheduler_log ORDER BY id DESC LIMIT 40"),
                                    "stats": memory.family_stats(conn), "weights": memory.weights(conn)})
        if path == "/api/events":
            return self._send(200, {"events": rows(conn, "SELECT * FROM events ORDER BY id DESC LIMIT ?", lim), "incidents": rows(conn, "SELECT * FROM incidents ORDER BY id DESC LIMIT 60"),
                                    "commands": rows(conn, "SELECT * FROM commands ORDER BY id DESC LIMIT 20")})
        if path == "/api/system":
            return self._send(200, {"system": sysinfo.snapshot(config.home()), "health": runtime.health(conn), "jobs_running": rows(conn, "SELECT id,type,lease_owner,started_at,heartbeat,progress,attempts FROM jobs WHERE status='running'"),
                                    "quarantined": rows(conn, "SELECT id,type,error,attempts FROM jobs WHERE status='quarantined' ORDER BY id DESC LIMIT 20"),
                                    "checkpoints": rows(conn, "SELECT * FROM checkpoints ORDER BY id DESC LIMIT 10"), "history": [json.loads(r[0]) | {"ts": r[1]} for r in conn.execute("SELECT data_json,ts FROM metrics_hist ORDER BY ts DESC LIMIT 180")][::-1]})
        if path == "/api/logs":
            name = os.path.basename(g("name", "supervisor"))
            lp = config.path("logs", "x.log").parent
            fs = sorted(p.name for p in lp.glob("*") if p.is_file())
            f = lp / name
            tail = ""
            if f.exists() and f.is_file():
                with open(f, "rb") as fh:
                    fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 40000)); tail = fh.read().decode("utf-8", "replace")
                tail = "\n".join(tail.splitlines()[-250:])
            return self._send(200, {"files": fs, "name": name, "tail": tail})
        if path == "/api/settings":
            return self._send(200, {"settings": db.settings(conn), "bounds": config.SAFE_SETTINGS, "trusted_hash": config.trusted_hash(), "trusted": config.trusted_config()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Nexus-Token") != token():
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 100000)) or b"{}")
            conn = db.connect()
            try:
                p = urllib.parse.urlparse(self.path).path
                if p == "/api/control":
                    cmd = body.get("command")
                    if cmd not in ("start", "pause", "resume", "stop", "restart_worker"):
                        return self._send(400, {"error": "unsupported command"})   # 'shutdown' is CLI-only: it would kill this dashboard
                    runtime.send_command(conn, cmd, body.get("args"))
                    return self._send(200, {"queued": cmd})
                if p == "/api/settings":
                    upd = config.validate_settings(body)
                    cur = db.settings(conn); cur.update(upd)
                    with db.tx(conn):
                        db.set_state(conn, "settings", json.dumps(cur))
                        db.event(conn, "settings", "web", f"settings changed {upd}")
                    return self._send(200, {"settings": cur})
                if p == "/api/simulate":
                    return self._send(200, simulate_api(conn, body))
                if p == "/api/backtest":
                    return self._send(200, adhoc_backtest(conn, body))
                self._send(404, {"error": "not found"})
            finally:
                conn.close()
        except (ValueError, KeyError) as e:
            self._send(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:
            LG.exception("POST %s", self.path)
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def serve(stop=None, port=PORT):
    token()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.timeout = 0.5
    LG.info("dashboard on http://127.0.0.1:%d", port)
    conn = db.connect()
    while not (stop and stop["v"]):
        srv.handle_request()
        try:
            conn.execute("UPDATE processes SET heartbeat=? WHERE name='web'", (time.time(),))
            if runtime.child_should_exit(conn):
                break
        except Exception:
            pass
    srv.server_close()
