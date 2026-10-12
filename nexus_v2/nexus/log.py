import logging, logging.handlers, sys
from . import config


def get(name: str) -> logging.Logger:
    lg = logging.getLogger(f"nexus.{name}")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    # rotating files bound log growth: 5 MB x 5 backups per component
    fh = logging.handlers.RotatingFileHandler(config.path("logs", f"{name}.log"), maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    if sys.stderr and sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        lg.addHandler(sh)
    lg.propagate = False
    return lg
