"""Dashboard numbers, sales reports and system statistics."""
import shutil
import time
from pathlib import Path

from . import clock
from .database import get_setting


def sales_today(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM sales WHERE sale_date=?",
                        (clock.local_date(),)).fetchone()[0]


def sales_total(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM sales").fetchone()[0]


def unassigned_total(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='unassigned'").fetchone()[0]


def sales_by_day(conn, days: int = 30):
    return conn.execute("SELECT sale_date, SUM(amount) AS total, COUNT(*) AS coins FROM sales "
                        "GROUP BY sale_date ORDER BY sale_date DESC LIMIT ?", (days,)).fetchall()


def recent_payments(conn, limit: int = 20):
    return conn.execute(
        "SELECT p.*, c.mac FROM payments p LEFT JOIN clients c ON c.id=p.client_id "
        "ORDER BY p.id DESC LIMIT ?", (limit,)).fetchall()


def connected_clients(conn, within: int = 300) -> int:
    return conn.execute("SELECT COUNT(*) FROM clients WHERE last_seen>?", (clock.now() - within,)).fetchone()[0]


def system_stats(path="/") -> dict:
    st = {}
    try:
        st["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except OSError:
        st["uptime_s"] = None
    try:
        st["cpu_temp_c"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        st["cpu_temp_c"] = None
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":")
            mem[k] = int(v.split()[0]) // 1024
        st["ram_total_mb"], st["ram_free_mb"] = mem["MemTotal"], mem["MemAvailable"]
    except (OSError, KeyError, ValueError):
        st["ram_total_mb"] = st["ram_free_mb"] = None
    try:
        du = shutil.disk_usage(path)
        st["disk_total_gb"], st["disk_used_gb"] = round(du.total / 1e9, 1), round(du.used / 1e9, 1)
    except OSError:
        st["disk_total_gb"] = st["disk_used_gb"] = None
    return st


def component_status(conn) -> dict:
    now = clock.now()
    hb = int(get_setting(conn, "last_heartbeat", 0))
    chb = int(get_setting(conn, "coin_daemon_heartbeat", 0))
    return {
        "monitor_alive": now - hb < 30,
        "coin_alive": now - chb < 30,
        "coin_mode": get_setting(conn, "coin_daemon_mode", "unknown"),
        "internet": get_setting(conn, "internet_status", "unknown"),
    }
