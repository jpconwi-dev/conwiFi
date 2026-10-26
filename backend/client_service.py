"""Client identification (IP -> MAC) and client records."""
import ipaddress
import re
import subprocess
from pathlib import Path

from . import clock

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def normalize_mac(mac: str) -> str | None:
    mac = (mac or "").strip().lower().replace("-", ":")
    return mac if MAC_RE.match(mac) else None


def sim_identity(n: int) -> tuple[str, str]:
    n = max(1, min(int(n), 200))
    return "02:00:00:00:00:%02x" % n, "10.0.0.%d" % (100 + n)


def mac_for_ip(ip: str) -> str | None:
    """Look up the MAC of a LAN host from the kernel neighbour table."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    try:
        out = subprocess.run(["ip", "neigh", "show", ip], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"lladdr\s+([0-9a-fA-F:]{17})", out)
        if m:
            return normalize_mac(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
            parts = line.split()
            if parts and parts[0] == ip:
                return normalize_mac(parts[3])
    except OSError:
        pass
    return None


def upsert_client(conn, mac: str, ip: str | None, hostname: str | None = None):
    now = clock.now()
    conn.execute("INSERT INTO clients(mac,ip,hostname,first_seen,last_seen) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip, last_seen=excluded.last_seen, "
                 "hostname=COALESCE(excluded.hostname, clients.hostname)",
                 (mac, ip, hostname, now, now))
    return conn.execute("SELECT * FROM clients WHERE mac=?", (mac,)).fetchone()


def in_subnet(ip: str, lan_ip: str, prefix: int) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(f"{lan_ip}/{prefix}", strict=False)
    except ValueError:
        return False


def refresh_ips_from_leases(conn, lease_file: str = "/var/lib/misc/dnsmasq.leases"):
    """dnsmasq lease file: expiry mac ip hostname clientid. Keeps client IPs current for tc rules."""
    try:
        text = Path(lease_file).read_text()
    except OSError:
        return
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 4:
            mac = normalize_mac(p[1])
            if mac:
                host = None if p[3] == "*" else p[3]
                upsert_client(conn, mac, p[2], host)
