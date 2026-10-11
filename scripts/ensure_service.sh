#!/bin/sh
# Start the research service if its heartbeat is stale (idempotent; the service has its own pid lock). Used by watchdog jobs.
cd "$(dirname "$0")/.." || exit 1
export STRATEGYLAB_HOME="${STRATEGYLAB_HOME:-$PWD/runs/real}"
python3 - <<'PY' || exit 0
import time, sys
from strategylab.db import DB
d = DB()
if d.get("control") == "running" and time.time() - d.get("service_heartbeat", 0) > 30: sys.exit(1)
PY
nohup python3 -m strategylab.service >> "$STRATEGYLAB_HOME/service.log" 2>&1 &
echo "service (re)started"
