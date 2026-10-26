"""Coin claims, rate calculation and transactional payment crediting."""
import json

from . import clock, session_service
from .database import get_setting, tx

CLAIM_GRACE_SECONDS = 15


def calculate_seconds(rates, amount: int) -> int:
    """rates: iterable of (amount, minutes). Largest bundles first; any remainder is prorated
    at the smallest bundle's rate."""
    rates = sorted([(int(a), int(m)) for a, m in rates if a > 0 and m > 0], reverse=True)
    if not rates or amount <= 0:
        return 0
    remaining, secs = int(amount), 0
    for a, m in rates:
        n, remaining = divmod(remaining, a)
        secs += n * m * 60
    if remaining:
        a, m = rates[-1]
        secs += (remaining * m * 60) // a
    return secs


def enabled_rates(conn):
    return [(r["amount"], r["minutes"]) for r in
            conn.execute("SELECT amount, minutes FROM rates WHERE enabled=1")]


def pulse_map(conn) -> dict:
    try:
        return {int(k): int(v) for k, v in json.loads(get_setting(conn, "pulse_map", "{}")).items()}
    except (ValueError, TypeError):
        return {}


def open_claim(conn, client_id: int, claim_seconds: int = 30):
    """Reserve the single coin slot for one customer. Returns (claim_row, error)."""
    now = clock.now()
    with tx(conn):
        conn.execute("UPDATE coin_claims SET status='expired' WHERE status='open' AND expires_at+?<?",
                     (CLAIM_GRACE_SECONDS, now))
        other = conn.execute("SELECT * FROM coin_claims WHERE status='open' AND client_id!=? "
                             "AND expires_at>=?", (client_id, now)).fetchone()
        if other:
            return None, "busy"
        conn.execute("UPDATE coin_claims SET status='closed' WHERE status='open' AND client_id=?",
                     (client_id,))
        cur = conn.execute("INSERT INTO coin_claims(client_id,created_at,expires_at) VALUES(?,?,?)",
                           (client_id, now, now + claim_seconds))
        return conn.execute("SELECT * FROM coin_claims WHERE id=?", (cur.lastrowid,)).fetchone(), None


def close_claim(conn, client_id: int):
    conn.execute("UPDATE coin_claims SET status='closed' WHERE status='open' AND client_id=?", (client_id,))


def current_claim(conn, client_id: int):
    return conn.execute("SELECT * FROM coin_claims WHERE status='open' AND client_id=? AND expires_at>=?",
                        (client_id, clock.now())).fetchone()


def record_coin(conn, event_uid: str, pulses, value, source: str, claim_seconds: int = 30) -> dict:
    """Record one physical coin atomically. Duplicate event_uid values are ignored, so a retried or
    replayed event can never credit twice."""
    ts = clock.now()
    with tx(conn):
        cur = conn.execute("INSERT OR IGNORE INTO coin_events(event_uid,pulses,value,source,created_at) "
                           "VALUES(?,?,?,?,?)", (event_uid, pulses, value, source, ts))
        if cur.rowcount == 0:
            return {"status": "duplicate"}
        event_id = cur.lastrowid
        if not value or value <= 0:
            conn.execute("UPDATE coin_events SET status='invalid' WHERE id=?", (event_id,))
            return {"status": "invalid"}
        claim = conn.execute("SELECT * FROM coin_claims WHERE status='open' AND expires_at+?>=? "
                             "ORDER BY id DESC LIMIT 1", (CLAIM_GRACE_SECONDS, ts)).fetchone()
        seconds = calculate_seconds(enabled_rates(conn), value) if claim else 0
        pay_uid = "pay-" + event_uid
        if not claim or seconds <= 0:
            p = conn.execute("INSERT INTO payments(payment_id,amount,pulses,status,source,created_at) "
                             "VALUES(?,?,?,'unassigned',?,?)", (pay_uid, value, pulses, source, ts))
            conn.execute("UPDATE coin_events SET status='unassigned', payment_id=? WHERE id=?",
                         (p.lastrowid, event_id))
            return {"status": "unassigned", "amount": value}
        session = session_service.credit(conn, claim["client_id"], seconds)
        p = conn.execute("INSERT INTO payments(payment_id,client_id,session_id,amount,pulses,"
                         "credited_seconds,status,source,created_at) VALUES(?,?,?,?,?,?,'credited',?,?)",
                         (pay_uid, claim["client_id"], session["id"], value, pulses, seconds, source, ts))
        conn.execute("INSERT INTO sales(payment_row_id,amount,sale_date,created_at) VALUES(?,?,?,?)",
                     (p.lastrowid, value, clock.local_date(ts), ts))
        conn.execute("UPDATE coin_events SET status='credited', payment_id=? WHERE id=?",
                     (p.lastrowid, event_id))
        conn.execute("UPDATE coin_claims SET expires_at=? WHERE id=?", (ts + claim_seconds, claim["id"]))
        return {"status": "credited", "amount": value, "seconds": seconds,
                "session_id": session["id"], "client_id": claim["client_id"]}
