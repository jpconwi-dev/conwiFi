"""jp-pisowifi command line: python -m backend.cli <command>"""
import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

try:
    import grp  # Linux/Unix only
except ImportError:  # Windows development machine
    grp = None

from . import auth, backup_service, client_service, clock, network_service as ns, payment_service, updater
from .config import VERSION, load_config, paths
from .database import (connect, get_setting, init_db, integrity_ok, log_event, migrate, schema_version,
                       set_setting)
from .recovery_service import startup_recovery, trust_clock_now

SERVICES = ["jp-pisowifi", "jp-pisowifi-monitor", "jp-pisowifi-coin", "dnsmasq"]


def fix_permissions(cfg):
    """Root-run commands must leave the data dir writable by the service account."""
    if grp is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        gid = grp.getgrnam("jp-pisowifi").gr_gid
    except KeyError:
        return
    d = paths(cfg)["data"]
    for p in [d, *d.rglob("*")]:
        try:
            os.chown(p, -1, gid)
            os.chmod(p, (0o2775 if p.is_dir() else 0o664) if p.name != "secret_key" else 0o640)
        except OSError:
            pass


def health_checks(cfg, conn):
    res = []
    add = lambda name, ok, crit, detail="": res.append((name, bool(ok), crit, detail))  # noqa: E731
    add("database integrity", integrity_ok(conn), True)
    add("schema version", schema_version(conn) >= 1, True, str(schema_version(conn)))
    hb = int(get_setting(conn, "last_heartbeat", 0))
    if not cfg["simulation_mode"]:
        for s in SERVICES:
            r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True)
            add(f"service {s}", r.stdout.strip() == "active", s != "jp-pisowifi-coin", r.stdout.strip())
        fw = ns.firewall_status()
        add("firewall table loaded", fw["loaded"], True)
        n = ns.effective_network(conn, cfg)
        r = subprocess.run(["ip", "-br", "addr", "show", n.get("lan_iface") or "none"], capture_output=True, text=True)
        add("LAN address assigned", n["lan_ip"] in r.stdout, True, r.stdout.strip())
        add("monitor heartbeat fresh", clock.now() - hb < 30, True)
    add("internet reachable", ns.internet_ok(cfg), False, get_setting(conn, "internet_status", "unknown"))
    return res


def cmd_init(cfg, a):
    conn = init_db(cfg)
    p = paths(cfg)
    if conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]:
        print("Admin account already exists; nothing changed.")
    else:
        pw = secrets.token_urlsafe(12)
        auth.create_admin(conn, "admin", pw, must_change=True)
        fd = os.open(p["credentials"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"username: admin\npassword: {pw}\n(You must change this at first login. Delete this file afterwards.)\n")
        print(f"Initial admin credentials  ->  username: admin   password: {pw}")
        print(f"(also saved to {p['credentials']})")
    fix_permissions(cfg)
    return 0


def cmd_migrate(cfg, a):
    conn = connect(paths(cfg)["db"])
    done = migrate(conn)
    print("applied migrations:", done or "none", "| schema version", schema_version(conn))
    fix_permissions(cfg)
    return 0


def cmd_health(cfg, a):
    conn = init_db(cfg)
    res = health_checks(cfg, conn)
    bad = [r for r in res if not r[1] and r[2]]
    if not a.quiet:
        for name, ok, crit, detail in res:
            print(f"[{'PASS' if ok else ('FAIL' if crit else 'WARN')}] {name} {detail}")
        print("RESULT:", "UNHEALTHY" if bad else "HEALTHY")
    return 1 if bad else 0


def cmd_status(cfg, a):
    conn = init_db(cfg)
    print(f"JP-PisoWiFi {VERSION} | simulation={cfg['simulation_mode']} | schema v{schema_version(conn)}")
    print("active sessions:", conn.execute("SELECT COUNT(*) FROM sessions WHERE status='active' AND expires_at>?",
                                           (clock.now(),)).fetchone()[0])
    print("internet:", get_setting(conn, "internet_status", "unknown"))
    return cmd_health(cfg, argparse.Namespace(quiet=False))


def cmd_backup(cfg, a):
    init_db(cfg).close()
    f = backup_service.create_backup(cfg, "manual")
    fix_permissions(cfg)
    print("backup written:", f)
    return 0


def cmd_restore(cfg, a):
    f = backup_service.restore_backup(cfg, Path(a.file), manage_services=not cfg["simulation_mode"])
    fix_permissions(cfg)
    print("restored. previous database saved as", f)
    return 0


def cmd_update(cfg, a):
    try:
        print(updater.run_update(cfg, a.version))
        return 0
    except updater.UpdateError as e:
        print("UPDATE FAILED:", e, file=sys.stderr)
        return 1


def cmd_update_from_request(cfg, a):
    req = paths(cfg)["update_request"]
    if not req.exists():
        return 0
    req.unlink()
    return cmd_update(cfg, argparse.Namespace(version=None))


def cmd_network_up(cfg, a):
    conn = init_db(cfg)
    n = ns.effective_network(conn, cfg)
    if not n.get("wan_iface") or not n.get("lan_iface"):
        w, l, note = ns.pick_interfaces(ns.detect_interfaces())
        print("Interfaces not configured;", note)
        if not (w and l):
            return 2
        n = {**n, "wan_iface": w, "lan_iface": l}
    errs = ns.apply_network({**cfg, "network": n})
    for e in errs:
        print("ERROR:", e, file=sys.stderr)
    log_event(conn, "error" if errs else "info", "network", "network-up " + ("failed: " + "; ".join(errs) if errs else "ok"))
    return 1 if errs else 0


def cmd_firewall_restore(cfg, a):
    ns.firewall_restore()
    print("JP-PisoWiFi firewall table removed. Customers are no longer filtered or NATed until 'network-up' runs.")
    return 0


def cmd_detect(cfg, a):
    ifs = ns.detect_interfaces()
    print(json.dumps(ifs, indent=2))
    print(ns.pick_interfaces(ifs)[2])
    return 0


def cmd_reset_password(cfg, a):
    conn = init_db(cfg)
    row = conn.execute("SELECT id FROM admins WHERE username=?", (a.username,)).fetchone()
    if not row:
        print("No such admin", file=sys.stderr)
        return 1
    pw = secrets.token_urlsafe(12)
    auth.set_password(conn, row["id"], pw)
    conn.execute("UPDATE admins SET must_change_password=1 WHERE id=?", (row["id"],))
    print(f"New temporary password for {a.username}: {pw}")
    fix_permissions(cfg)
    return 0


def cmd_admin_allow_mac(cfg, a):
    conn = init_db(cfg)
    mac = client_service.normalize_mac(a.mac)
    if not mac:
        print("Invalid MAC", file=sys.stderr)
        return 1
    macs = set(json.loads(get_setting(conn, "admin_lan_macs", "[]")))
    macs.add(mac)
    set_setting(conn, "admin_lan_macs", json.dumps(sorted(macs)))
    print("Allowed admin access from customer-side device", mac)
    return 0


def cmd_trust_clock(cfg, a):
    conn = init_db(cfg)
    print("System time is:", clock.local_date(), __import__("time").strftime("%H:%M:%S"))
    trust_clock_now(conn)
    print("Clock marked as trusted.")
    return 0


def cmd_sim_coin(cfg, a):
    if not (cfg["simulation_mode"] or cfg["coin"].get("test_mode")):
        print("sim-coin is disabled in production (needs simulation_mode or coin.test_mode).", file=sys.stderr)
        return 1
    conn = init_db(cfg)
    res = payment_service.record_coin(conn, secrets.token_hex(8), a.value, a.value, "simulation",
                                      int(cfg["coin"]["claim_seconds"]))
    print(res)
    return 0


def cmd_recover(cfg, a):
    conn = init_db(cfg)
    from .monitor import make_bw
    print(startup_recovery(conn, cfg, ns.get_backend(cfg), make_bw(cfg, conn)))
    return 0


COMMANDS = {
    "init": cmd_init, "migrate": cmd_migrate, "health": cmd_health, "status": cmd_status,
    "backup": cmd_backup, "restore": cmd_restore, "update": cmd_update,
    "update-from-request": cmd_update_from_request, "network-up": cmd_network_up,
    "firewall-restore": cmd_firewall_restore, "detect": cmd_detect, "reset-admin-password": cmd_reset_password,
    "admin-allow-mac": cmd_admin_allow_mac, "trust-clock": cmd_trust_clock, "sim-coin": cmd_sim_coin,
    "recover": cmd_recover,
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jp-pisowifi")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        sp = sub.add_parser(name)
        if name == "health":
            sp.add_argument("--quiet", action="store_true")
        if name == "restore":
            sp.add_argument("file")
        if name == "update":
            sp.add_argument("--version")
        if name == "reset-admin-password":
            sp.add_argument("--username", default="admin")
        if name == "admin-allow-mac":
            sp.add_argument("mac")
        if name == "sim-coin":
            sp.add_argument("value", type=int)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    return COMMANDS[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
