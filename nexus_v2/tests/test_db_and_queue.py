"""Gate B: migrations, transactions, integrity, backup/restore, job recovery, unique identities, counter reconciliation."""
import json, os, sqlite3, time
import pytest
from nexus import db, jobs, memory, research as R, strategies as S, config


def test_migrations_idempotent_and_wal(home):
    c = db.open_db()
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    v = db.migrate(c)
    assert v == max(db.MIGRATIONS) and db.migrate(c) == v


def test_transaction_rollback(conn):
    with pytest.raises(RuntimeError):
        with db.tx(conn):
            conn.execute("INSERT INTO events(ts,type,component,message) VALUES(1,'t','c','m')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_integrity_and_backup_restore(conn, home):
    with db.tx(conn):
        db.event(conn, "t", "c", "keep me")
    b = db.backup(conn, kind="manual")
    assert b["ok"]
    with db.tx(conn):
        conn.execute("DELETE FROM events")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    conn.close()
    db.restore(b["path"])
    c2 = db.open_db()
    assert c2.execute("SELECT COUNT(*) FROM events WHERE message='keep me'").fetchone()[0] == 1
    assert db.integrity_check(c2)[0]


def test_corrupt_backup_refused(conn, home, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(Exception):
        db.restore(str(bad))


def test_job_lifecycle_retry_and_quarantine(conn):
    with db.tx(conn):
        jid = jobs.enqueue(conn, "experiment", {"x": 1}, dedupe="k1", max_attempts=2)
        assert jobs.enqueue(conn, "experiment", {"x": 1}, dedupe="k1") is None      # dedupe
    j = jobs.claim(conn, "w1", os.getpid())
    assert j["id"] == jid and j["attempts"] == 1
    assert jobs.fail(conn, jid, "e1") == "retry"
    conn.execute("UPDATE jobs SET not_before=0")
    j = jobs.claim(conn, "w1", os.getpid())
    assert jobs.fail(conn, jid, "e2") == "quarantined"
    assert jobs.counts(conn)["quarantined"] == 1
    assert jobs.claim(conn, "w1", os.getpid()) is None


def test_interrupted_job_recovered_after_worker_death(conn):
    with db.tx(conn):
        jid = jobs.enqueue(conn, "experiment", {}, dedupe="k2")
    j = jobs.claim(conn, "dead-worker", 999999)           # a pid that does not exist
    rc = jobs.reclaim_stale(conn, 90.0)
    assert rc and rc[0][0] == jid
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "pending"


def test_live_slow_job_not_killed(conn):
    with db.tx(conn):
        jid = jobs.enqueue(conn, "validate", {}, dedupe="k3")
    jobs.claim(conn, "w", os.getpid())                     # live pid, fresh heartbeat, fresh progress
    assert jobs.reclaim_stale(conn, 90.0, progress_timeout=900) == []
    conn.execute("UPDATE jobs SET progress=? WHERE id=?", (json.dumps({"msg": "x", "ts": time.time() - 5000}), jid))
    rc = jobs.reclaim_stale(conn, 90.0, progress_timeout=900)   # heartbeating but no progress for >timeout -> stuck
    assert rc and "no progress" in rc[0][2]


def test_experiment_unique_identity_and_no_double_count(conn, home):
    import conftest
    for s, seed in (("ETHUSDT", 1), ("BTCUSDT", 2)):
        conftest.write_dataset(home, s, conftest.synth_arrays(6000, seed))
    sp = S.make_spec("donchian", "ETHUSDT", "1h", {"n": 20})
    r = R.evaluate(conn, sp)
    sid, new1 = R.persist(conn, sp, r, "t")
    sid2, new2 = R.persist(conn, sp, r, "t")               # replay after a crash: must not double count
    assert new1 and not new2
    k = memory.counters(conn)
    assert k["experiments_completed"] == 1 and k["unique_strategies"] == 1
    assert k["backtests"] == len(r["runs"])


def test_counters_reconcile_with_raw_rows(conn, home):
    import conftest
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(6000, 3)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(6000, 4))
    for n in (20, 40, 80):
        sp = S.make_spec("donchian", "ETHUSDT", "1h", {"n": n})
        R.persist(conn, sp, R.evaluate(conn, sp), "t")
    k = memory.counters(conn)
    assert k["closed_trades"] == conn.execute("SELECT SUM(trades) FROM backtest_runs").fetchone()[0]
    assert k["experiments_completed"] == conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 3


def test_rebaseline_frozen_after_research(conn, home):
    import conftest
    db.rebaseline(conn)                                    # allowed on a fresh DB
    conftest.write_dataset(home, "ETHUSDT", conftest.synth_arrays(6000, 3)); conftest.write_dataset(home, "BTCUSDT", conftest.synth_arrays(6000, 4))
    sp = S.make_spec("donchian", "ETHUSDT", "1h", {"n": 20})
    R.persist(conn, sp, R.evaluate(conn, sp), "t")
    with pytest.raises(PermissionError):
        db.rebaseline(conn)
