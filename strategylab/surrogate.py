"""Learning from rejected strategies: Bayesian-ridge surrogate predicting a candidate's TRAIN-period net edge (R/trade) from its spec.
Trained only on development-stage (screen) outcomes of everything already tested -- including all the rejects -- so the search spends compute on
specs the data says are likelier to carry cost-adjusted edge, plus an uncertainty bonus so unexplored regions still get probed.
Never sees validation/locked-test metrics."""
from __future__ import annotations
import json, time
import numpy as np

STOP_B = (0.5, 1, 1.5, 2, 3, 4, 6); HOLD_B = (6, 12, 24, 48, 96, 192, 400)


def _b(v, edges):
    return sum(v > e for e in edges)


def feats(spec, sy, tf):
    p, fam = spec.get("params") or {}, spec["family"]; side = spec.get("side", "both")
    stop, rr, hold = float(spec.get("stop_atr") or 0), float(spec.get("target_rr") or 0), float(spec.get("max_hold") or 0)
    f = ["bias", f"fam:{fam}", f"sy:{sy}", f"tf:{tf}", f"side:{side}", f"tf_fam:{tf}:{fam}", f"sy_fam:{sy}:{fam}", f"tf_side:{tf}:{side}", f"fam_side:{fam}:{side}",
         f"tf_stop:{tf}:{_b(stop, STOP_B)}", f"fam_stop:{fam}:{_b(stop, STOP_B)}", f"tf_hold:{tf}:{_b(hold, HOLD_B)}", f"fam_hold:{fam}:{_b(hold, HOLD_B)}",
         f"tf_rr:{tf}:{'none' if not rr else _b(rr, (1, 1.5, 2, 3))}", f"fam_rr:{fam}:{'none' if not rr else _b(rr, (1, 1.5, 2, 3))}"]
    for k, v in sorted(p.items()):
        if isinstance(v, (list, tuple)): v = "|".join(map(str, v))
        f += [f"p:{fam}:{k}={v}", f"tfp:{tf}:{fam}:{k}={v}"]
    for x in spec.get("filters") or []: f.append(f"flt:{x.get('type')}"); f.append(f"tf_flt:{tf}:{x.get('type')}")
    if not spec.get("filters"): f.append("flt:none")
    return f


class Surrogate:
    def __init__(self, db, ttl=900):
        self.db, self.ttl, self.t = db, ttl, 0.0; self.idx = {}; self.w = None; self.Ainv = None; self.stats = {}

    def _vec(self, fl):
        x = np.zeros(len(self.idx)); 
        for k in fl:
            i = self.idx.get(k)
            if i is not None: x[i] = 1.0
        return x

    def fit(self, limit=40000):
        rows = self.db.q("""SELECT s.spec_json sj, e.symbol sy, e.timeframe tf, e.metrics_json m FROM experiments e JOIN strategies s ON s.id=e.strategy_id
                            WHERE e.stage='screen' AND e.synthetic=0 AND e.timeframe!='4h' ORDER BY e.id DESC LIMIT ?""", (limit,))
        data = []
        for r in rows:
            try:
                m = json.loads(r["m"]); y = m.get("mean_R"); n = m.get("n", 0)
                if y is None or n < 30: continue
                data.append((feats(json.loads(r["sj"]), r["sy"], r["tf"]), float(np.clip(y, -0.5, 0.5)), min(n, 300) / 300.0))
            except Exception: continue
        if len(data) < 300: self.w = None; self.t = time.time(); return False
        data.reverse(); cnt = {}
        for fl, _, _ in data:
            for k in fl: cnt[k] = cnt.get(k, 0) + 1
        self.idx = {k: i for i, k in enumerate(sorted(k for k, c in cnt.items() if c >= 3))}
        X = np.zeros((len(data), len(self.idx)), dtype=np.float32); y = np.array([d[1] for d in data]); wt = np.array([d[2] for d in data])
        for i, (fl, _, _) in enumerate(data):
            for k in fl:
                j = self.idx.get(k)
                if j is not None: X[i, j] = 1.0
        cut = int(len(data) * 0.8); lam = 5.0

        def solve(Xa, ya, wa):
            A = (Xa.T * wa) @ Xa + lam * np.eye(Xa.shape[1]); return np.linalg.solve(A, (Xa.T * wa) @ ya), A
        w_tr, _ = solve(X[:cut].astype(np.float64), y[:cut], wt[:cut]); pred = X[cut:] @ w_tr; act = y[cut:]
        order = np.argsort(pred)[::-1]; top = order[:max(10, len(order) // 10)]
        corr = float(np.corrcoef(pred, act)[0, 1]) if pred.std() > 0 else 0.0
        self.stats = dict(n=len(data), holdout_corr=round(corr, 3), top_decile_mean_R=round(float(act[top].mean()), 4), all_mean_R=round(float(act.mean()), 4),
                          top_decile_hit=round(float((act[top] > 0.05).mean()), 3), base_hit=round(float((act > 0.05).mean()), 3), features=len(self.idx), ts=time.time())
        self.w, A = solve(X.astype(np.float64), y, wt); self.Ainv = np.linalg.inv(A); self.t = time.time()
        try: self.db.set("surrogate_stats", self.stats)
        except Exception: pass
        return True

    def ready(self):
        if self.w is None or time.time() - self.t > self.ttl:
            try: self.fit()
            except Exception: self.w = None; self.t = time.time()
        return self.w is not None

    def score(self, spec, sy, tf, kappa=0.6):
        """Optimistic predicted net R/trade (mean + kappa*sd)."""
        if not self.ready(): return 0.0
        x = self._vec(feats(spec, sy, tf)); mu = float(x @ self.w); sd = float(np.sqrt(max(x @ self.Ainv @ x, 0))) * 0.15
        return mu + kappa * sd
