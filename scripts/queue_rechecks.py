"""Queue placebo (beta-control) rechecks for already-promoted strategies."""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategylab.db import DB
d = DB(); n = 0
for r in d.q("SELECT strategy_id sid, symbol, timeframe FROM strategy_status WHERE synthetic=0 AND status IN ('PROMISING','BACKTEST_QUALIFIED') AND summary_json NOT LIKE '%placebo%'"):
    d.enqueue("recheck", {"strategy_id": r["sid"], "symbol": r["symbol"], "timeframe": r["timeframe"]}, priority=3); n += 1
d.enqueue("portfolio", {}, priority=4)
print("queued", n, "rechecks + 1 portfolio build")
