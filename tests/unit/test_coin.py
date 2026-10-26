import unittest

from tests.helpers import TempEnv  # noqa: F401  (sets sys.path)
from backend.coin_service import PulseGrouper, map_pulses, process_group
from backend import payment_service


class PulseTests(unittest.TestCase):
    def test_counts_pulses_in_group(self):
        g = PulseGrouper(debounce_ms=20, pulse_timeout_ms=400)
        for t in (0.0, 0.1, 0.2, 0.3, 0.4):
            self.assertTrue(g.edge(t))
        self.assertIsNone(g.poll(0.6))       # still inside timeout
        self.assertEqual(g.poll(0.9), 5)     # group closed
        self.assertIsNone(g.poll(2.0))       # nothing left

    def test_debounce_ignores_bounce(self):
        g = PulseGrouper(debounce_ms=20, pulse_timeout_ms=400)
        self.assertTrue(g.edge(0.000))
        self.assertFalse(g.edge(0.005))      # contact bounce
        self.assertFalse(g.edge(0.015))
        self.assertTrue(g.edge(0.100))
        self.assertEqual(g.poll(1.0), 2)

    def test_two_coins_are_separate_groups(self):
        g = PulseGrouper(20, 400)
        g.edge(0.0)
        self.assertEqual(g.poll(0.5), 1)
        g.edge(1.0); g.edge(1.1)
        self.assertEqual(g.poll(1.6), 2)

    def test_denomination_mapping(self):
        pm = {1: 1, 5: 5, 10: 10}
        self.assertEqual(map_pulses(5, pm), 5)
        self.assertIsNone(map_pulses(7, pm))


class ProcessGroupTests(TempEnv):
    def test_unknown_pulse_count_is_recorded_invalid_and_not_credited(self):
        res = process_group(self.conn, self.cfg, 7, "simulation")
        self.assertEqual(res["status"], "invalid")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM coin_events").fetchone()[0], "invalid")

    def test_known_pulses_credit_open_claim(self):
        c = self.make_client()
        payment_service.open_claim(self.conn, c["id"])
        res = process_group(self.conn, self.cfg, 5, "simulation")
        self.assertEqual(res["status"], "credited")
        self.assertEqual(res["seconds"], 3600)


if __name__ == "__main__":
    unittest.main()
