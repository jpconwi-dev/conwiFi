"""SQLite access, migrations, settings and event logging."""
import contextlib
import json
import sqlite3
from pathlib import Path

from . import clock
from .config import ROOT, ensure_dirs, paths

DEFAULT_SETTINGS = {
    "business_name": "JP-PisoWiFi",
    "wifi_name": "conwiFi",
    "tagline": "Insert coins to access the internet.",
    "currency": "\u20b1",
    "primary_color": "#c4861a",
    "background_color": "#12233f",
    "text_color": "#f4f1e8",
    "support_contact": "",
    "instructions": "Tap Insert Coin, then drop your coins within the time shown. "
                    "Your internet time starts as soon as the coin is counted.",
    "logo_file": "",
    "maintenance_mode": "0",
    "max_session_minutes": "720",
    "default_down_kbps": "0",
    "default_up_kbps": "0",
    "pulse_map": json.dumps({"1": 1, "5": 5, "10": 10, "20": 20}),
    "admin_lan_macs": "[]",
    "last_heartbeat": "0",
    "internet_status": "unknown",
    "internet_checked_at": "0",
    "coin_daemon_heartbeat": "0",
    "coin_daemon_mode": "unknown",
    "last_update_result": "",
    "last_backup_at": "0",
    "pause_enabled": "1",
    "max_pause_minutes": "60",
    "dashboard_title": "You are online",
    "dashboard_message": "",
    "sounds_enabled": "1",
    "sound_volume": "80",
    "low_time_seconds": "60",
    "snd_coin": "", "snd_connected": "", "snd_low_time": "",
    "snd_expired": "", "snd_paused": "", "snd_resumed": "",
}

SOUND_EVENTS = {
    "coin": "Coin counted",
    "connected": "Connected / time added",
    "low_time": "Time almost over",
    "expired": "Time is up",
    "paused": "Session paused",
    "resumed": "Session resumed",
}

DEFAULT_RATES = [(1, 10), (5, 60), (10, 150), (20, 300)]


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")  # payments must survive power loss
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextlib.contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migration_files(base: Path | None = None):
    base = Path(base) if base else ROOT / "database"
    files = [(1, "schema", base / "schema.sql")]
    mdir = base / "migrations"
    if mdir.exists():
        for f in sorted(mdir.glob("[0-9][0-9][0-9]_*.sql")):
            files.append((int(f.name[:3]), f.stem, f))
    return sorted(files)


def migrate(conn, base: Path | None = None) -> list[int]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(version INTEGER PRIMARY KEY, name TEXT, applied_at INTEGER NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, path in migration_files(base):
        if version in done:
            continue
        sql = path.read_text()
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + sql)
            conn.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                         (version, name, clock.now()))
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def schema_version(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if r is not None:
        return r[0]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (key, str(value), clock.now()))


def all_settings(conn) -> dict:
    out = dict(DEFAULT_SETTINGS)
    out.update({r[0]: r[1] for r in conn.execute("SELECT key,value FROM settings")})
    return out


def seed_defaults(conn):
    """Insert missing defaults only; never overwrites existing data."""
    with tx(conn):
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                         (k, v, clock.now()))
        seeded = conn.execute("SELECT 1 FROM settings WHERE key='rates_seeded'").fetchone()
        if not seeded:
            for i, (amt, mins) in enumerate(DEFAULT_RATES):
                conn.execute("INSERT INTO rates(amount,minutes,enabled,sort_order) VALUES(?,?,1,?)",
                             (amt, mins, i))
            conn.execute("INSERT INTO settings(key,value,updated_at) VALUES('rates_seeded','1',?)",
                         (clock.now(),))


def init_db(cfg: dict) -> sqlite3.Connection:
    ensure_dirs(cfg)
    conn = connect(paths(cfg)["db"])
    migrate(conn)
    seed_defaults(conn)
    return conn


def log_event(conn, level: str, source: str, message: str):
    try:
        conn.execute("INSERT INTO system_events(level,source,message,created_at) VALUES(?,?,?,?)",
                     (level, source, message[:500], clock.now()))
    except sqlite3.Error:
        pass


def integrity_ok(conn) -> bool:
    return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
