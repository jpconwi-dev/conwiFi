"""Entry point for the coin-processing service."""
import os
import signal
import sys

from .coin_service import run_daemon
from .config import load_config
from .database import init_db, log_event


def main():
    os.umask(0o002)
    cfg = load_config()
    conn = init_db(cfg)
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(v=True))
    try:
        log_event(conn, "info", "coin", "coin service starting (simulation=%s)" % cfg["simulation_mode"])
        run_daemon(cfg, conn, stop=lambda: stop["v"])
    except RuntimeError as e:
        log_event(conn, "error", "coin", f"coin service cannot start: {e}")
        print(f"coin service cannot start: {e}", file=sys.stderr)
        return 3  # systemd unit uses RestartPreventExitStatus=3 so a misconfiguration is not retried forever
    return 0


if __name__ == "__main__":
    sys.exit(main())
