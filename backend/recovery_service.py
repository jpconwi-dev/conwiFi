"""Boot/outage recovery and the periodic reconcile loop that makes network state match SQLite.

Design: web and coin processes only write to SQLite. This reconcile step (run by the monitor
service, which holds CAP_NET_ADMIN) is the only place that grants or revokes network access."""
from . import clock, session_service
from .bandwidth_service import BandwidthManager
from .database import get_setting, log_event, set_setting

CLOCK_TOLERANCE = 5  # seconds


def clock_trusted(conn, cfg) -> tuple[bool, str]:
    """The Orange Pi One has no RTC. After a power cut the clock may restart in the past, which would
    hand out free time. Trust the clock only if it is plausible and not behind the last heartbeat."""
    now = clock.now()
    if now < int(cfg["min_valid_epoch"]):
        return False, "system clock is before the minimum valid date"
    hb = int(get_setting(conn, "last_heartbeat", 0))
    if now + CLOCK_TOLERANCE < hb:
        return False, "system clock is behind the last recorded heartbeat (clock reset after power loss?)"
    return True, "ok"


def trust_clock_now(conn):
    """Admin override after manually verifying the date/time."""
    set_setting(conn, "last_heartbeat", clock.now())
    log_event(conn, "warning", "recovery", "administrator confirmed the system clock")


def reconcile(conn, cfg, backend, bw: BandwidthManager, state: dict) -> dict:
    """One pass. Returns a summary dict (also useful for tests)."""
    summary = {"authorized": 0, "revoked": 0, "expired": 0, "clock_ok": True}
    ok, why = clock_trusted(conn, cfg)
    summary["clock_ok"] = ok
    if not ok:
        if state.get("clock_ok", True):
            log_event(conn, "error", "recovery", f"Clock untrusted, blocking all customers: {why}")
        state["clock_ok"] = False
        for mac in list(backend.list_authorized()):
            backend.revoke(mac)
            summary["revoked"] += 1
        bw.sync({})
        return summary
    if not state.get("clock_ok", True):
        log_event(conn, "info", "recovery", "Clock trusted again, resuming normal operation")
    state["clock_ok"] = True
    set_setting(conn, "last_heartbeat", clock.now())

    expired = session_service.expire_due(conn)
    summary["expired"] = len(expired)

    maintenance = get_setting(conn, "maintenance_mode", "0") == "1"
    desired, wanted_bw = {}, {}
    if not maintenance:
        for s in session_service.active_sessions(conn):
            blocked = conn.execute("SELECT blocked FROM clients WHERE id=?", (s["client_id"],)).fetchone()
            if blocked and blocked["blocked"]:
                continue
            desired[s["mac"]] = s
            if s["ip"]:
                wanted_bw[s["client_id"]] = (s["ip"], s["down_kbps"], s["up_kbps"])

    current = backend.list_authorized()
    ips = {r["mac"]: r["ip"] for r in conn.execute("SELECT mac, ip FROM clients")}
    for mac in list(current):
        if mac not in desired:
            backend.revoke(mac, ips.get(mac))
            summary["revoked"] += 1
    for mac, s in desired.items():
        remaining = session_service.remaining_seconds(s)
        if mac not in current or s["authorized_expiry"] != s["expires_at"]:
            backend.authorize(mac, remaining + 5)
            conn.execute("UPDATE sessions SET authorized_expiry=? WHERE id=?", (s["expires_at"], s["id"]))
            summary["authorized"] += 1
    bw.sync(wanted_bw)
    return summary


def startup_recovery(conn, cfg, backend, bw) -> dict:
    """Run once at service start: rebuild kernel state from the database."""
    backend.flush()
    bw.reset()
    conn.execute("UPDATE sessions SET authorized_expiry=NULL WHERE status='active'")
    conn.execute("UPDATE coin_claims SET status='expired' WHERE status='open'")
    state = {"clock_ok": True}
    res = reconcile(conn, cfg, backend, bw, state)
    log_event(conn, "info", "recovery",
              f"Startup recovery: authorized={res['authorized']} expired={res['expired']} clock_ok={res['clock_ok']}")
    return res
