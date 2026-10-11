import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sys
from strategylab import data
if __name__ == "__main__":
    sym = sys.argv[1]; start = sys.argv[2] if len(sys.argv) > 2 else "2019-09"
    data.fetch_symbol(sym, "1m", start)
