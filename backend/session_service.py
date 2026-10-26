"""Sessions store an absolute expiry timestamp in SQLite (never only an in-memory countdown)."""
from . import clock
from .database import get_setting


def active_session(conn, client_id):
    return conn.execute(
        "SELECT * FROM sessions WHERE client_id=? AND status='active' AND expires_at>? "
        "ORDER BY id DESC LIMIT 1", (client_id, clock.now())).fetchone()


def remaining_seconds(session, now=None) -> int:
    now = clock.now() if now is None else now
    return max(0, int(session["expires_at"]) - now)


def credit(conn, client_id: int, seconds: int):
    """Extend the active session or open a new one. Caller owns the transaction."""
    now = clock.now()
    s = active_session(conn, client_id)
    if s:
        conn.execute("UPDATE sessions SET expires_at=expires_at+?, purchased_seconds=purchased_seconds+? "
                     "WHERE id=?", (seconds, seconds, s["id"]))
        return conn.execute("SELECT * FROM sessions WHERE id=?", (s["id"],)).fetchone()
    # close any stale 'active' rows for this client so there is only one live session
    conn.execute("UPDATE sessions SET status='expired', ended_at=? WHERE client_id=? AND status='active'",
                 (now, client_id))
    cur = conn.execute(
        "INSERT INTO sessions(client_id,started_at,expires_at,purchased_seconds,status,down_kbps,up_kbps) "
        "VALUES(?,?,?,?, 'active', ?, ?)",
        (client_id, now, now + seconds, seconds,
         int(get_setting(conn, "default_down_kbps", 0)), int(get_setting(conn, "default_up_kbps", 0))))
    return conn.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()


def end_session(conn, session_id: int, status: str = "ended"):
    conn.execute("UPDATE sessions SET status=?, ended_at=? WHERE id=? AND status='active'",
                 (status, clock.now(), session_id))


def expire_due(conn) -> list:
    now = clock.now()
    rows = conn.execute("SELECT * FROM sessions WHERE status='active' AND expires_at<=?", (now,)).fetchall()
    for r in rows:
        conn.execute("UPDATE sessions SET status='expired', ended_at=? WHERE id=?", (now, r["id"]))
    return rows


def active_sessions(conn):
    return conn.execute(
        "SELECT s.*, c.mac, c.ip, c.hostname FROM sessions s JOIN clients c ON c.id=s.client_id "
        "WHERE s.status='active' AND s.expires_at>? ORDER BY s.expires_at", (clock.now(),)).fetchall()
