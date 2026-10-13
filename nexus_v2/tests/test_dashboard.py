"""Gate F: dashboard routes, 15 goals by full name, counters reconcile, dead runtime never green, request safety."""
import json, os, shutil, subprocess, threading, time, urllib.request, urllib.error, pathlib
import pytest
from nexus import config, db, server, runtime, goals, memory, research as R, strategies as S, validation as V, exchange
import conftest

def _find_browser():
    cands = list(pathlib.Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):                      # Windows: Chrome or Edge (both Chromium)
        base = os.environ.get(env)
        if base:
            cands += [pathlib.Path(base) / "Google/Chrome/Application/chrome.exe", pathlib.Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    return next((c for c in cands if c.exists()), None)


CHROME = _find_browser()


@pytest.fixture()
def web(conn, home):
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(40000, 7)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(40000, 8))
    conn.execute("INSERT INTO datasets(symbol,kind,timeframe,rows,start_ts,end_ts,gaps,dupes,bad_rows,version,data_class,manifest_json) VALUES('ETHUSDT','klines','1h',40000,0,1,0,0,0,'v','verified','{\"funding_events\":1}')")
    with db.tx(conn):
        exchange.seed(conn)
    sp = S.make_spec("ema_cross", "ETHUSDT", "1h", {"fast": 12, "slow": 50, "stop_atr": 3.0, "max_hold": 96})
    R.persist(conn, sp, R.evaluate(conn, sp), "t")
    sid = S.canonical_id(sp); V.run_validation(conn, sid)
    stop = {"v": False}
    srv_port = 18000 + os.getpid() % 1000
    th = threading.Thread(target=server.serve, args=(stop, srv_port), daemon=True); th.start()
    time.sleep(0.8)
    yield conn, f"http://127.0.0.1:{srv_port}", sid, home
    stop["v"] = True


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def test_all_api_routes_ok(web):
    conn, base, sid, _ = web
    for p in ("health", "overview", "goals", "strategies", f"strategy/{sid}", "experiments", "agents", "guardians", "datasets", "validation",
              "results", "memory", "events", "system", "logs", "settings"):
        assert isinstance(get(f"{base}/api/{p}"), (dict, list)), p


def test_goals_api_has_all_15_full_names_and_fields(web):
    conn, base, sid, _ = web
    for url in (f"{base}/api/goals", f"{base}/api/strategy/{sid}"):
        d = get(url)
        gl = d["goals"]
        assert [g["name"] for g in gl] == [n for _, n, _ in config.GOALS] and len(gl) == 15
        for g in gl:
            for f in ("target", "actual", "symbol", "status", "evidence", "stage", "explanation", "obstacle", "evaluated_at", "progress"):
                assert f in g
            assert g["symbol"] in ("✓", "◐", "✕", "?") and g["actual"]


def test_goals_never_pass_unverified_assumptions(web):
    conn, base, sid, _ = web
    gl = {g["n"]: g for g in get(f"{base}/api/goals")["goals"]}
    assert gl[2]["symbol"] == "?" and gl[3]["symbol"] == "?" and gl[4]["symbol"] == "?"    # BTCC specs / leverage unverified
    assert gl[15]["symbol"] != "✓" and gl[14]["symbol"] != "✓"                              # no paper-forward, no guardian verification
    for n in (7, 8, 9, 10):
        assert gl[n]["symbol"] != "✓"                                                       # $300 cannot evidence $2,000+/month


def test_counters_reconcile_with_database(web):
    conn, base, sid, _ = web
    o = get(f"{base}/api/overview")
    q = lambda s: conn.execute(s).fetchone()[0]
    c = o["counters"]
    assert c["experiments_completed"] == q("SELECT COUNT(*) FROM experiments")
    assert c["backtests"] == q("SELECT COUNT(*) FROM backtest_runs")
    assert c["closed_trades"] == q("SELECT COALESCE(SUM(trades),0) FROM backtest_runs")
    assert c["unique_strategies"] == q("SELECT COUNT(DISTINCT id) FROM strategies")


def test_dead_runtime_is_never_shown_healthy(web):
    conn, base, sid, _ = web
    db.set_state(conn, "desired", "running")                                 # campaign wants to run, but no supervisor exists
    h = get(f"{base}/api/health")
    assert h["status"] == "dead" and not h["supervisor_alive"] and any("supervisor" in r for r in h["reasons"])
    db.set_state(conn, "supervisor_heartbeat", time.time())                  # fresh heartbeat but no lock owner -> still not alive
    assert runtime.health(conn)["status"] == "dead"


def test_post_security(web):
    conn, base, sid, _ = web
    def post(path, body, token=True, host=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Nexus-Token"] = server.token()
        if host:
            h["Host"] = host
        req = urllib.request.Request(base + path, json.dumps(body).encode(), h)
        try:
            return urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code
    assert post("/api/control", {"command": "pause"}, token=False) == 403
    assert post("/api/control", {"command": "pause"}, host="evil.example") == 403
    assert post("/api/control", {"command": "shutdown"}) == 400             # cannot kill the supervisor from the browser
    assert post("/api/control", {"command": "rm -rf"}) == 400
    assert post("/api/control", {"command": "pause"}) == 200
    assert post("/api/settings", {"workers": 99}) == 400                     # outside safe bounds
    assert post("/api/settings", {"goals": 1}) == 400                        # trusted config is not editable
    assert post("/api/settings", {"workers": 1}) == 200
    assert get(f"{base}/api/settings")["settings"]["workers"] == 1


def test_adhoc_backtest_is_exploration_only_and_unrecorded(web):
    conn, base, sid, _ = web
    n0 = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    req = urllib.request.Request(base + "/api/backtest", json.dumps({"family": "donchian", "params": {"n": 40}}).encode(),
                                 {"Content-Type": "application/json", "X-Nexus-Token": server.token()})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    assert r["recorded"] is False and "holdout inaccessible" in r["region"]
    assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == n0
    assert conn.execute("SELECT COUNT(*) FROM holdout_access").fetchone()[0] == 1       # only the validation stage's own look


def test_simulator_separates_deposits_from_profit(web):
    conn, base, sid, _ = web
    req = lambda b: json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/simulate", json.dumps(b).encode(),
                       {"Content-Type": "application/json", "X-Nexus-Token": server.token()}), timeout=30).read())
    z = req({"win_rate": 0.4, "avg_win_r": 2.0, "avg_loss_r": 1.0, "deposits": 5000, "paths": 300, "risk_pct": 0.0, "months": 3})
    assert all(m["p05"] == m["p50"] == m["p95"] == 0.0 for m in z["monthly_profit_quantiles"])      # deposits are never profit
    assert z["equity_quantiles"][2]["p50"] == pytest.approx(300 + 3 * 5000)                           # ...but they do raise equity
    w = req({"win_rate": 0.55, "avg_win_r": 2.0, "avg_loss_r": 1.0, "withdrawal": 1000, "paths": 400, "trades_per_month": 30})
    assert "HYPOTHETICAL" in w["basis"] and "50%" in w["retention_check"]


@pytest.mark.skipif(CHROME is None, reason="chromium not installed")
def test_rendered_dom_shows_nav_and_all_goal_names_and_escapes_html(web):
    conn, base, sid, home = web
    with db.tx(conn):
        conn.execute("INSERT INTO hypotheses(agent,family,symbol,timeframe,text,rationale,falsifier,status,created_at,updated_at) VALUES('x','f','ETHUSDT','1h',?,?,?,?,?,?)",
                     ("<b id=pwn>boom</b><img src=x onerror=document.title='pwned'>", "r", "f", "open", 1.0, 1.0))
    def dom(page):
        return subprocess.run([str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=8000", "--dump-dom", f"{base}/#/{page}"],
                              capture_output=True, text=True, timeout=90).stdout
    d = dom("financial-goals")
    for _, name, _t in config.GOALS:
        assert name in d, name
    for n in server.NAV:
        assert f'data-n="{n}"' in d
    assert "Starting Capital" in d and ("✓" in d and "?" in d)
    m = dom("research-memory")
    assert 'id="pwn"' not in m and "&lt;b id=pwn&gt;" in m and "pwned" not in m.split("<title>")[1].split("</title>")[0]
    dash = dom("dashboard")
    assert "DISABLED" in dash and "Closed simulated trades" in dash
