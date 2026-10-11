"""Build the tracker page: python scripts/build_tracker.py status.json out.html"""
import sys, json
tpl = open(__file__.replace("scripts/build_tracker.py", "tracker/tracker.template.html")).read()
data = json.load(open(sys.argv[1]))
open(sys.argv[2], "w").write(tpl.replace("/*DATA*/", "window.__STATUS__ = " + json.dumps(data) + ";"))
