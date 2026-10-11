import sys
from . import dashboard, service
if __name__ == "__main__":
    {"service": service.main, "dashboard": dashboard.serve}.get(sys.argv[1] if len(sys.argv) > 1 else "dashboard", dashboard.serve)()
