"""Generate the persistent research-efficiency report: docs/EFFICIENCY_REPORT.md + a row in the DB `reports` table."""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategylab.db import DB
from strategylab import audit
o = audit.run_audit(DB()); md = audit.render_markdown(o)
out = os.path.join(os.path.dirname(__file__), "..", "docs", "EFFICIENCY_REPORT.md"); open(out, "w").write(md); print(md)
