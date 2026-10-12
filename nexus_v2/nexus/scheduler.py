"""Adaptive research scheduler (runs in its own process). Chooses WHAT to investigate next from persisted memory,
within bounded budgets, and records why."""
import json, random, time

from . import agents as A, config, db, jobs, memory, research as R, strategies as S, ENGINE_VERSION

BATCH = 25


def _seen(conn):
    return {(r[0], r[1]) for r in conn.execute("SELECT strategy_id,dataset_version FROM experiments WHERE engine_version=?", (ENGINE_VERSION,))} | \
           {tuple(r[0].split(":")[1:3]) for r in conn.execute("SELECT dedupe_key FROM jobs WHERE dedupe_key LIKE 'exp:%' AND status IN ('pending','running')")}


def agent_used_last_hour(conn, name):
    return conn.execute("SELECT COUNT(*) FROM scheduler_log WHERE agent=? AND ts>? AND mode!='skip'", (name, time.time() - 3600)).fetchone()[0]


def agent_used_last(conn, name, seconds):
    return conn.execute("SELECT COUNT(*) FROM scheduler_log WHERE agent=? AND ts>? AND mode!='skip'", (name, time.time() - seconds)).fetchone()[0]


def tick(conn, rng=None):
    """One scheduling round. Returns number of experiments enqueued."""
    rng = rng or random.Random()
    st = db.settings(conn)
    if db.get_state(conn, "desired") != "running" or db.get_state(conn, "throttle", "none") == "high":
        return 0
    if not conn.execute("SELECT 1 FROM datasets LIMIT 1").fetchone():
        return 0
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending' AND type='experiment'").fetchone()[0]
    room = st["queue_high_water"] - pending
    if room <= 0:
        return 0
    seen = _seen(conn)
    ags = A.all_agents(conn)
    rng.shuffle(ags)
    total = 0
    for ag in ags:
        # hourly cap, paced: at most 1/6 of the hourly budget per rolling 10 minutes (no bursts that starve other work)
        left = min(st["agent_budget_per_hour"] - agent_used_last_hour(conn, ag.name),
                   max(1, st["agent_budget_per_hour"] // 6) - agent_used_last(conn, ag.name, 600))
        n = min(BATCH, left, room)
        if n <= 0:
            continue
        props = ag.propose(n, rng, seen)
        if not props:
            with db.tx(conn):
                conn.execute("INSERT INTO scheduler_log(ts,agent,family,mode,why,would_change,strategy_id) VALUES(?,?,?,?,?,?,?)",
                             (time.time(), ag.name, None, "skip", "no unseen/eligible proposals (family space exhausted or prerequisites missing)",
                              "stagnation guardian will redirect budget", None))
            continue
        with db.tx(conn):
            for p in props:
                sid = S.canonical_id(p.spec)
                ver = R.explore_version(conn, p.spec)
                hid = R.hypothesis_for(conn, ag.name, p.spec["family"], p.spec["symbol"], p.spec["timeframe"])
                jid = jobs.enqueue(conn, "experiment", {"spec": p.spec, "agent": ag.name, "hypothesis_id": hid, "why": p.why},
                                   priority=5, dedupe=f"exp:{sid}:{ver}")
                if jid is None:
                    continue
                conn.execute("INSERT INTO scheduler_log(ts,agent,family,mode,why,would_change,strategy_id) VALUES(?,?,?,?,?,?,?)",
                             (time.time(), ag.name, p.spec["family"], p.mode, p.why, p.would_change, sid))
                total += 1
                room -= 1
    return total


def periodic(conn):
    """Time-bucketed housekeeping jobs (idempotent via dedupe keys)."""
    now = time.time()
    with db.tx(conn):
        jobs.enqueue(conn, "data_update", {}, priority=2, dedupe=f"data:{int(now // (6 * 3600))}")
        jobs.enqueue(conn, "paper_forward", {}, priority=4, dedupe=f"pf:{int(now // 86400)}")
        for r in conn.execute("SELECT c.strategy_id FROM candidates c WHERE NOT EXISTS (SELECT 1 FROM validations v WHERE "
                              "v.strategy_id=c.strategy_id AND v.stage='adversarial') LIMIT 5"):
            jobs.enqueue(conn, "challenge", {"strategy_id": r[0]}, priority=4, dedupe=f"challenge:{r[0]}")
        # adversarial agent recommendations -> bounded, reversible budget redirection (never zero)
        adv = A.Adversarial(conn)
        for rec in adv.recommend():
            w = memory.weights(conn).get(rec["family"], 1.0)
            if w > 0.3:
                memory.set_weight(conn, rec["family"], w * 0.5, "A10 recommendation: " + rec["reason"])
                db.event(conn, "scheduler", "scheduler", f"reduced effort on {rec['family']}: {rec['reason']}")
