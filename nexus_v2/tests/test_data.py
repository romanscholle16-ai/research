"""Gate C (data): integrity checks, chronological handling, resumable downloads, holdout access control."""
import hashlib, http.server, io, os, threading, zipfile
import numpy as np, pytest
from nexus import data, db, config, validation as V, research as R, strategies as S
import conftest


def _zip(rows, header=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", ("open_time,open,high,low,close,volume,close_time,quote_volume,count,tbv,tbq,ignore\n" if header else "") +
                   "\n".join(",".join(str(v) for v in r) for r in rows))
    return buf.getvalue()


def test_validate_klines_dupes_gaps_badrows():
    t0 = 1_700_000_000_000 // 3_600_000 * 3_600_000
    rows = []
    for i in [0, 1, 1, 2, 5, 6]:                            # duplicate bar 1, gap of 2 bars (3,4)
        rows.append([t0 + i * 3_600_000, 100, 101, 99, 100, 10, 0, 0, 0, 5, 0, 0])
    rows.append([t0 + 7 * 3_600_000, 100, 90, 99, 100, 10, 0, 0, 0, 5, 0, 0])   # high below close -> bad
    a, st = data.validate_klines(np.array(rows, float), 3_600_000)
    assert st["dupes"] == 1 and st["bad_rows"] == 1 and st["missing_bars"] == 2 and st["gap_events"] == 1
    assert (np.diff(a[:, 0]) > 0).all()


def test_header_and_microsecond_timestamps(tmp_path):
    t0 = 1_700_000_000_000 // 3_600_000 * 3_600_000
    p = tmp_path / "a.zip"
    p.write_bytes(_zip([[t0 * 1000, 1, 2, 0.5, 1, 1, 0, 0, 0, 0, 0, 0]]))
    a = data.parse_klines([p])
    assert a[0, 0] == t0                                     # microseconds normalised to ms


def test_aggregation_drops_partial_bars_and_respects_boundaries():
    k = conftest.synth_arrays(103, 1)
    agg = data._aggregate(k, 4 * 3_600_000)
    assert all(agg[:, 0] % (4 * 3_600_000) == 0)             # aligned to UTC 4h boundaries
    assert len(agg) <= 103 // 4 + 1
    blk = k[(k[:, 0] >= agg[1, 0]) & (k[:, 0] < agg[1, 0] + 4 * 3_600_000)]
    assert agg[1, 2] == blk[:, 2].max() and agg[1, 3] == blk[:, 3].min() and agg[1, 1] == blk[0, 1] and agg[1, 4] == blk[-1, 4]


class _Srv(http.server.BaseHTTPRequestHandler):
    payload = b""; truncate_first = True; hits = 0
    def log_message(self, *a): pass
    def do_GET(self):
        cls = type(self)
        if self.path.endswith(".CHECKSUM"):
            body = (hashlib.sha256(cls.payload).hexdigest() + "  f.zip\n").encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        cls.hits += 1
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = cls.payload[start:]
            self.send_response(206); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if cls.truncate_first:
            cls.truncate_first = False
            self.send_response(200); self.send_header("Content-Length", str(len(cls.payload))); self.end_headers()
            self.wfile.write(cls.payload[: len(cls.payload) // 2]); self.wfile.flush(); self.connection.close(); return
        self.send_response(200); self.send_header("Content-Length", str(len(cls.payload))); self.end_headers(); self.wfile.write(cls.payload)


def test_interrupted_download_resumes_and_verifies_checksum(tmp_path):
    _Srv.payload = os.urandom(200_000); _Srv.truncate_first = True; _Srv.hits = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Srv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dest = tmp_path / "f.zip"
    r = data.download_file(f"http://127.0.0.1:{srv.server_port}/f.zip", dest, sleep=lambda s: None)
    srv.shutdown()
    assert r == "ok:verified" and dest.read_bytes() == _Srv.payload and not (tmp_path / "f.zip.part").exists()


def test_checksum_mismatch_rejected(tmp_path):
    _Srv.payload = b"abc" * 1000; _Srv.truncate_first = False
    class Bad(_Srv):
        def do_GET(self):
            if self.path.endswith(".CHECKSUM"):
                body = b"0" * 64 + b"  f.zip\n"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            else:
                super().do_GET()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    r = data.download_file(f"http://127.0.0.1:{srv.server_port}/f.zip", tmp_path / "f.zip", retries=2, sleep=lambda s: None)
    srv.shutdown()
    assert r == "failed" and not (tmp_path / "f.zip").exists()


@pytest.fixture()
def world(conn, home):
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(20000, 1)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(20000, 2))
    sp = S.make_spec("ema_cross", "ETHUSDT", "1h", {"fast": 12, "slow": 50, "stop_atr": 3.0, "max_hold": 96})
    sid = S.canonical_id(sp)
    R.persist(conn, sp, R.evaluate(conn, sp), "t")
    ns = S.normalize(sp)
    m, split = R.get_market(conn, ns)
    return conn, sid, m, split


def test_holdout_denied_without_prior_gates_and_logged(world):
    conn, sid, m, split = world
    with pytest.raises(data.HoldoutDenied):
        data.holdout_region(conn, m, split, sid, "peek", "validation.holdout_stage")
    assert conn.execute("SELECT COUNT(*) FROM holdout_access WHERE passed=-1").fetchone()[0] == 1


def test_holdout_denied_for_unauthorised_caller(world):
    conn, sid, m, split = world
    with pytest.raises(data.HoldoutDenied):
        data.holdout_region(conn, m, split, sid, "agent wants to tune", "agent.A1")
    assert "unauthorised" in conn.execute("SELECT purpose FROM holdout_access").fetchone()[0]


def test_holdout_single_look_and_budget(world):
    conn, sid, m, split = world
    for st in ("walkforward", "robustness"):
        conn.execute("INSERT INTO validations(strategy_id,stage,passed,n_trades,metrics_json,thresholds_hash,engine_version,dataset_version,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (sid, st, 1, 1, "{}", config.trusted_hash(), "x", "v", 1.0))
    data.holdout_region(conn, m, split, sid, "final", "validation.holdout_stage")
    conn.execute("UPDATE holdout_access SET passed=1 WHERE strategy_id=?", (sid,))
    with pytest.raises(data.HoldoutDenied, match="single look"):
        data.holdout_region(conn, m, split, sid, "again", "validation.holdout_stage")


def test_walkforward_region_is_purged_from_selection_data(world):
    conn, sid, m, split = world
    ns = S.normalize(S.make_spec("ema_cross", "ETHUSDT", "1h", {"fast": 12, "slow": 50, "stop_atr": 3.0, "max_hold": 96}))
    passed, mt, runs, res_all = V.walkforward(conn, sid, ns, m, split)
    assert mt["region"][0] == split["explore_end"] + 96      # purge = max_hold bars
    for res, pm in res_all:                                  # no trade enters before the purged start
        if len(res.trades["entry_i"]):
            assert res.trades["entry_i"].min() >= split["explore_end"] + 96
            assert res.trades["exit_i"].max() < split["dev_end"]


def test_fixed_split_survives_new_bars(conn, home):
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(20000, 1)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(20000, 2))
    sp = S.make_spec("donchian", "ETHUSDT", "1h", {"n": 20})
    ns = S.normalize(sp)
    m1, s1 = R.get_market(conn, ns)
    v1 = R.explore_version(conn, ns)
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(20500, 1)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(20500, 2))
    data._CACHE.clear(); R._EV.clear()
    m2, s2 = R.get_market(conn, ns)
    assert (s2["explore_end_ts"], s2["dev_end_ts"], s2["holdout_end_ts"]) == (s1["explore_end_ts"], s1["dev_end_ts"], s1["holdout_end_ts"])
    assert s2["holdout_end"] == s1["holdout_end"] and s2["n"] == 20500      # extra bars lie AFTER the holdout (forward-only)
    assert R.explore_version(conn, ns) == v1               # explore-region identity unchanged -> no re-running settled experiments
