"""Admin authentication, password hashing, login rate limiting and CSRF."""
import hmac
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from . import clock
from .database import tx

MIN_PASSWORD_LEN = 8


def hash_password(pw: str) -> str:
    return generate_password_hash(pw)  # scrypt in Werkzeug 3


def verify_password(h: str, pw: str) -> bool:
    return check_password_hash(h, pw)


def validate_new_password(pw: str) -> str | None:
    if len(pw) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if pw.lower() in {"password", "12345678", "admin123", "admin1234", "qwertyui"}:
        return "That password is too common."
    return None


def create_admin(conn, username: str, password: str, must_change: bool = False, role="owner") -> int:
    with tx(conn):
        cur = conn.execute("INSERT INTO admins(username,password_hash,role,must_change_password,created_at) "
                           "VALUES(?,?,?,?,?)",
                           (username, hash_password(password), role, int(must_change), clock.now()))
        return cur.lastrowid


def set_password(conn, admin_id: int, password: str):
    with tx(conn):
        conn.execute("UPDATE admins SET password_hash=?, must_change_password=0 WHERE id=?",
                     (hash_password(password), admin_id))


def too_many_attempts(conn, key: str, max_failures: int, window_minutes: int) -> bool:
    since = clock.now() - window_minutes * 60
    n = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND ts>?", (key, since)).fetchone()[0]
    return n >= max_failures


def record_failure(conn, key: str):
    conn.execute("INSERT INTO login_attempts(key,ts) VALUES(?,?)", (key, clock.now()))
    conn.execute("DELETE FROM login_attempts WHERE ts<?", (clock.now() - 86400,))


def clear_failures(conn, key: str):
    conn.execute("DELETE FROM login_attempts WHERE key=?", (key,))


def authenticate(conn, username: str, password: str):
    row = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
    if row is None:
        # burn comparable time so usernames can't be probed by timing
        check_password_hash(generate_password_hash("x"), password)
        return None
    if not verify_password(row["password_hash"], password):
        return None
    conn.execute("UPDATE admins SET last_login_at=? WHERE id=?", (clock.now(), row["id"]))
    return row


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(expected: str | None, supplied: str | None) -> bool:
    return bool(expected) and bool(supplied) and hmac.compare_digest(expected, supplied)


def audit(conn, admin, action: str, detail: str = "", ip: str = ""):
    conn.execute("INSERT INTO audit_logs(admin_id,username,action,detail,ip,created_at) VALUES(?,?,?,?,?,?)",
                 (admin["id"] if admin else None, admin["username"] if admin else None,
                  action, detail[:500], ip, clock.now()))
