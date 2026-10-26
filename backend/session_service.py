"""Sessions store an absolute expiry timestamp in SQLite (never only an in-memory countdown)."""
from . import clock
from .database import get_setting


def active_session(conn, client_id):
    """The customer's RUNNING (not paused) session."""
    return conn.execute(
        "SELECT * FROM sessions WHERE client_id=? AND status='active' AND paused_at IS NULL "
        "AND expires_at>? ORDER BY id DESC LIMIT 1", (client_id, clock.now())).fetchone()


def current_session(conn, client_id):
    """Running or paused session."""
    return conn.execute(
        "SELECT * FROM sessions WHERE client_id=? AND status='active' "
        "AND (paused_at IS NOT NULL OR expires_at>?) ORDER BY id DESC LIMIT 1",
        (client_id, clock.now())).fetchone()


def is_paused(session) -> bool:
    return session["paused_at"] is not None


def remaining_seconds(session, now=None) -> int:
    if session["paused_at"] is not None:
        return max(0, int(session["paused_remaining"] or 0))
    now = clock.now() if now is None else now
    return max(0, int(session["expires_at"]) - now)


def max_pause_seconds(conn) -> int:
    return int(get_setting(conn, "max_pause_minutes", 60)) * 60


def pause(conn, client_id: int):
    """Freeze the remaining time. Network access is removed by the monitor on its next pass."""
    if get_setting(conn, "pause_enabled", "1") != "1":
        return False, "Pausing is turned off."
    s = active_session(conn, client_id)
    if not s:
        return False, "You have no running session to pause."
    now = clock.now()
    conn.execute("UPDATE sessions SET paused_at=?, paused_remaining=? WHERE id=?",
                 (now, remaining_seconds(s, now), s["id"]))
    return True, None


def resume(conn, client_id: int):
    s = current_session(conn, client_id)
    if not s or s["paused_at"] is None:
        return False, "Your session is not paused."
    now = clock.now()
    if now - s["paused_at"] > max_pause_seconds(conn):
        conn.execute("UPDATE sessions SET status='expired', ended_at=? WHERE id=?", (now, s["id"]))
        return False, "The pause limit was reached, so the session ended."
    conn.execute("UPDATE sessions SET expires_at=?, paused_at=NULL, paused_remaining=NULL, "
                 "authorized_expiry=NULL WHERE id=?", (now + int(s["paused_remaining"] or 0), s["id"]))
    return True, None


def credit(conn, client_id: int, seconds: int):
    """Extend the active session or open a new one. Caller owns the transaction."""
    now = clock.now()
    s = current_session(conn, client_id)
    if s and s["paused_at"] is not None:
        # a coin while paused adds the time and resumes the session
        rem = int(s["paused_remaining"] or 0) + seconds
        conn.execute("UPDATE sessions SET expires_at=?, purchased_seconds=purchased_seconds+?, paused_at=NULL, "
                     "paused_remaining=NULL, authorized_expiry=NULL WHERE id=?", (now + rem, seconds, s["id"]))
        return conn.execute("SELECT * FROM sessions WHERE id=?", (s["id"],)).fetchone()
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
    rows = conn.execute("SELECT * FROM sessions WHERE status='active' AND paused_at IS NULL AND expires_at<=?",
                        (now,)).fetchall()
    rows += conn.execute("SELECT * FROM sessions WHERE status='active' AND paused_at IS NOT NULL "
                         "AND paused_at+?<=?", (max_pause_seconds(conn), now)).fetchall()
    for r in rows:
        conn.execute("UPDATE sessions SET status='expired', ended_at=? WHERE id=?", (now, r["id"]))
    return rows


def active_sessions(conn):
    return conn.execute(
        "SELECT s.*, c.mac, c.ip, c.hostname FROM sessions s JOIN clients c ON c.id=s.client_id "
        "WHERE s.status='active' AND s.paused_at IS NULL AND s.expires_at>? ORDER BY s.expires_at",
        (clock.now(),)).fetchall()


def paused_sessions(conn):
    return conn.execute(
        "SELECT s.*, c.mac, c.ip, c.hostname FROM sessions s JOIN clients c ON c.id=s.client_id "
        "WHERE s.status='active' AND s.paused_at IS NOT NULL ORDER BY s.paused_at").fetchall()
