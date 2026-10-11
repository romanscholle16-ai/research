#!/bin/sh
# Start the research service only if control=running and its heartbeat is stale. The service also holds a pid lock.
cd "$(dirname "$0")/.." || exit 1
export STRATEGYLAB_HOME="${STRATEGYLAB_HOME:-$PWD/runs/real}"
if python3 - <<'PY'
import time, sys
from strategylab.db import DB
d = DB()
healthy = d.get("control") != "running" or time.time() - d.get("service_heartbeat", 0) < 30
sys.exit(0 if healthy else 1)
PY
then echo "service healthy (no action)"; exit 0; fi
nohup python3 -m strategylab.service >> "$STRATEGYLAB_HOME/service.log" 2>&1 &
echo "service restarted"
