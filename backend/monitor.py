"""Privileged monitor service: reconcile network access with SQLite, check internet, back up, and
roll back unconfirmed network changes."""
import json
import os
import signal
import sys
import time

from . import backup_service, clock, client_service, network_service as ns
from .bandwidth_service import BandwidthManager
from .config import load_config, paths
from .database import get_setting, init_db, log_event, set_setting
from .recovery_service import reconcile, startup_recovery

LOOP_SECONDS = 2


def make_bw(cfg, conn):
    net = ns.effective_network(conn, cfg)
    return BandwidthManager(net.get("lan_iface") or "lan0", cfg["simulation_mode"], paths(cfg)["sim_tc"])


def tick(conn, cfg, backend, bw, state, now_mono=None):
    now_mono = time.monotonic() if now_mono is None else now_mono
    summary = reconcile(conn, cfg, backend, bw, state)

    # network change requested from the admin panel
    if get_setting(conn, "network_apply_requested", "0") == "1":
        set_setting(conn, "network_apply_requested", "0")
        errs = ns.apply_network({**cfg, "network": ns.effective_network(conn, cfg)})
        log_event(conn, "error" if errs else "info", "network",
                  "Network change failed: " + "; ".join(errs) if errs else "Network change applied (awaiting confirmation)")
        bw.lan = ns.effective_network(conn, cfg).get("lan_iface") or bw.lan
        bw.reset()
    p = ns.rollback_due(conn, clock.now())
    if p:
        set_setting(conn, "network_override", json.dumps(p["previous"]))
        set_setting(conn, "network_pending", "")
        errs = ns.apply_network({**cfg, "network": ns.effective_network(conn, cfg)})
        log_event(conn, "warning", "network", "Network change not confirmed in time: rolled back"
                  + (f" (apply errors: {errs})" if errs else ""))
        bw.reset()

    if now_mono - state.get("last_inet", -999) > 15:
        state["last_inet"] = now_mono
        ok = ns.internet_ok(cfg)
        if state.get("inet_ok") is not None and state["inet_ok"] != ok:
            log_event(conn, "info" if ok else "warning", "network",
                      "Internet connectivity restored" if ok else "Internet connectivity lost (LAN still up)")
        state["inet_ok"] = ok
        set_setting(conn, "internet_status", "online" if ok else "offline")
        set_setting(conn, "internet_checked_at", clock.now())
        if not cfg["simulation_mode"]:
            client_service.refresh_ips_from_leases(conn)

    interval = int(cfg["backup"]["interval_hours"]) * 3600
    last = int(get_setting(conn, "last_backup_at", 0))
    if summary["clock_ok"] and clock.now() - last >= interval:
        try:
            backup_service.create_backup(cfg, "auto")
            set_setting(conn, "last_backup_at", clock.now())
        except Exception as e:  # noqa: BLE001
            log_event(conn, "error", "backup", f"Automatic backup failed: {e}")
            set_setting(conn, "last_backup_at", clock.now() - interval + 3600)  # retry in an hour
    return summary


def main():
    os.umask(0o002)
    cfg = load_config()
    conn = init_db(cfg)
    backend = ns.get_backend(cfg)
    bw = make_bw(cfg, conn)
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(v=True))
    startup_recovery(conn, cfg, backend, bw)
    state = {"clock_ok": True}
    while not stop["v"]:
        try:
            tick(conn, cfg, backend, bw, state)
        except Exception as e:  # noqa: BLE001  never die on a single bad pass
            log_event(conn, "error", "monitor", f"tick failed: {e!r}")
        time.sleep(LOOP_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
