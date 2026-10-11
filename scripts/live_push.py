"""Write the next live-feed 'tick' document for the Live Test Feed artifact.
Usage: live_push.py out_tick.json   -> prints the tick doc id and the id to prune (if any)."""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategylab.db import DB

home = os.environ.get("STRATEGYLAB_HOME", "")
seqf = os.path.join(home, "live_seq.json")
st = json.load(open(seqf)) if os.path.exists(seqf) else {"seq": 0, "last_exp_id": 0, "last_ts": None}
d = DB(); now = time.time()
exp_max = d.q("SELECT COALESCE(MAX(id),0) m FROM experiments WHERE synthetic=0")[0]["m"]
# one row per strategy outcome finished since last tick (newest 40), with the stage it reached
rows = d.q("""SELECT ss.strategy_id sid, ss.symbol, ss.timeframe tf, ss.status, ss.updated, ss.summary_json sj, s.family
              FROM strategy_status ss JOIN strategies s ON s.id=ss.strategy_id
              WHERE ss.synthetic=0 AND ss.updated>? ORDER BY ss.updated DESC LIMIT 40""", (st["last_ts"] or now - 600,))
n_since = d.q("SELECT COUNT(*) n FROM strategy_status WHERE synthetic=0 AND updated>?", (st["last_ts"] or now - 600,))[0]["n"]
feed = []
for r in rows:
    sm = json.loads(r["sj"]); stage = "final test" if sm.get("final") else "validation" if sm.get("validate") else "screen"
    m = sm.get("final", {}).get("test") or sm.get("validate", {}).get("val") or sm.get("screen") or {}
    feed.append(dict(id=r["sid"], family=r["family"], sym=r["symbol"].replace("USDT", ""), tf=r["tf"], status=r["status"], stage=stage,
                     trades=m.get("n", 0), R=round(m.get("mean_R", 0), 3), why=[x for x in sm.get("reasons", [])][:3], age=round(now - r["updated"], 1)))
cj = d.get("current_job"); nxt = d.q("SELECT payload FROM jobs WHERE status='queued' AND kind='experiment' ORDER BY priority,id LIMIT 3")
q = lambda p: (lambda x: dict(family=x["spec"]["family"], sym=x["symbol"].replace("USDT", ""), tf=x["timeframe"], note=x.get("note", "")))(json.loads(p))
counts = d.counts(0)
seq = st["seq"] + 1
tick = dict(seq=seq, ts=now, control=d.get("control", "stopped"), svc_age=round(now - d.get("service_heartbeat", 0), 1),
            running=(dict(kind=cj["kind"], sym=(cj.get("symbol") or "").replace("USDT", ""), tf=cj.get("timeframe"), note=cj.get("note", ""), elapsed=round(now - cj["since"], 1)) if cj else None),
            next=[q(x["payload"]) for x in nxt], feed=feed, completed_since_last=n_since, since_secs=round(now - st["last_ts"], 1) if st["last_ts"] else None,
            totals=dict(experiments=counts.get("experiments", 0), promising=counts.get("PROMISING", 0), rejected=counts.get("REJECTED", 0), qualified=counts.get("BACKTEST_QUALIFIED", 0)),
            errors_1h=d.q("SELECT COUNT(*) n FROM events WHERE level='error' AND ts>?", (now - 3600,))[0]["n"])
json.dump(tick, open(sys.argv[1], "w"))
json.dump(dict(seq=seq, last_exp_id=exp_max, last_ts=now), open(seqf, "w"))
print(f"tick t{seq:07d} prune t{seq-30:07d}" if seq > 30 else f"tick t{seq:07d}")
