import unittest

from tests.helpers import TempEnv
from backend import clock, payment_service as ps, session_service as ss


class RateTests(unittest.TestCase):
    RATES = [(1, 10), (5, 60), (10, 150), (20, 300)]

    def test_exact_rates(self):
        for amt, mins in self.RATES:
            self.assertEqual(ps.calculate_seconds(self.RATES, amt), mins * 60)

    def test_combined_amount_uses_largest_bundles(self):
        self.assertEqual(ps.calculate_seconds(self.RATES, 36), (300 + 150 + 60 + 10) * 60)

    def test_prorates_remainder_and_handles_empty(self):
        self.assertEqual(ps.calculate_seconds([(5, 60)], 7), 60 * 60 + (2 * 60 * 60) // 5)
        self.assertEqual(ps.calculate_seconds([], 5), 0)
        self.assertEqual(ps.calculate_seconds(self.RATES, 0), 0)


class PaymentTests(TempEnv):
    def test_credit_creates_session_payment_and_sale(self):
        c = self.make_client()
        ps.open_claim(self.conn, c["id"])
        res = ps.record_coin(self.conn, "e1", 5, 5, "simulation")
        self.assertEqual(res["status"], "credited")
        s = ss.active_session(self.conn, c["id"])
        self.assertAlmostEqual(ss.remaining_seconds(s), 3600, delta=2)
        self.assertEqual(self.conn.execute("SELECT SUM(amount) FROM sales").fetchone()[0], 5)
        p = self.conn.execute("SELECT * FROM payments").fetchone()
        self.assertEqual((p["status"], p["credited_seconds"], p["session_id"]), ("credited", 3600, s["id"]))

    def test_duplicate_event_never_credits_twice(self):
        c = self.make_client()
        ps.open_claim(self.conn, c["id"])
        self.assertEqual(ps.record_coin(self.conn, "same", 5, 5, "gpio")["status"], "credited")
        self.assertEqual(ps.record_coin(self.conn, "same", 5, 5, "gpio")["status"], "duplicate")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT SUM(amount) FROM sales").fetchone()[0], 5)

    def test_coin_without_claim_is_kept_as_unassigned(self):
        res = ps.record_coin(self.conn, "x1", 1, 1, "gpio")
        self.assertEqual(res["status"], "unassigned")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT amount FROM payments WHERE status='unassigned'").fetchone()[0], 1)

    def test_second_coin_extends_same_session(self):
        c = self.make_client()
        ps.open_claim(self.conn, c["id"])
        ps.record_coin(self.conn, "a", 1, 1, "s")
        ps.record_coin(self.conn, "b", 1, 1, "s")
        rows = self.conn.execute("SELECT * FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purchased_seconds"], 1200)

    def test_only_one_customer_can_hold_the_coin_slot(self):
        a, b = self.make_client(1), self.make_client(2)
        self.assertIsNone(ps.open_claim(self.conn, a["id"])[1])
        self.assertEqual(ps.open_claim(self.conn, b["id"])[1], "busy")
        ps.close_claim(self.conn, a["id"])
        self.assertIsNone(ps.open_claim(self.conn, b["id"])[1])

    def test_claim_expires_and_late_coin_is_unassigned(self):
        c = self.make_client()
        ps.open_claim(self.conn, c["id"], claim_seconds=30)
        clock.set_offset(30 + ps.CLAIM_GRACE_SECONDS + 5)
        self.assertEqual(ps.record_coin(self.conn, "late", 1, 1, "s")["status"], "unassigned")

    def test_invalid_value_is_not_credited(self):
        c = self.make_client()
        ps.open_claim(self.conn, c["id"])
        self.assertEqual(ps.record_coin(self.conn, "bad", 9, None, "s")["status"], "invalid")


class SessionTests(TempEnv):
    def test_expiry_marks_session_expired(self):
        c = self.make_client()
        ps.open_claim(self.conn, c["id"])
        ps.record_coin(self.conn, "e", 1, 1, "s")           # 10 minutes
        clock.set_offset(601)
        self.assertEqual(len(ss.expire_due(self.conn)), 1)
        self.assertIsNone(ss.active_session(self.conn, c["id"]))

    def test_time_extension_adds_to_expiry_not_to_now(self):
        c = self.make_client()
        with self.conn:
            pass
        ps.open_claim(self.conn, c["id"])
        ps.record_coin(self.conn, "e1", 1, 1, "s")
        before = ss.active_session(self.conn, c["id"])["expires_at"]
        ps.record_coin(self.conn, "e2", 5, 5, "s")
        self.assertEqual(ss.active_session(self.conn, c["id"])["expires_at"], before + 3600)


if __name__ == "__main__":
    unittest.main()
