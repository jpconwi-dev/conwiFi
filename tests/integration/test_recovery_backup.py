import unittest

from tests.helpers import TempEnv
from backend import backup_service, clock, network_service as ns, payment_service as ps, session_service as ss
from backend.bandwidth_service import BandwidthManager
from backend.database import connect, set_setting
from backend.recovery_service import clock_trusted, reconcile, startup_recovery, trust_clock_now


class RecoveryTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.backend = ns.SimBackend(self.cfg)
        self.bw = BandwidthManager("lan0", True, None)
        self.c = self.make_client()
        ps.open_claim(self.conn, self.c["id"])
        ps.record_coin(self.conn, "ev1", 5, 5, "simulation")  # 60 min

    def test_paying_customer_gets_authorized_by_reconcile(self):
        res = reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        self.assertEqual(res["authorized"], 1)
        self.assertIn(self.c["mac"], self.backend.list_authorized())
        res2 = reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        self.assertEqual(res2["authorized"], 0)  # idempotent

    def test_reboot_restores_valid_session_and_drops_expired(self):
        reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        other = self.make_client(2)
        ps.close_claim(self.conn, self.c["id"])   # first customer releases the coin slot
        ps.open_claim(self.conn, other["id"])
        ps.record_coin(self.conn, "ev2", 1, 1, "simulation")  # 10 min
        # simulate power loss: kernel set is empty, 30 minutes pass, clock keeps running
        self.backend.flush()
        clock.set_offset(30 * 60)
        res = startup_recovery(self.conn, self.cfg, self.backend, BandwidthManager("lan0", True, None))
        auth = self.backend.list_authorized()
        self.assertIn(self.c["mac"], auth)          # 30 min left -> restored
        self.assertNotIn(other["mac"], auth)        # expired during the outage -> not restored
        self.assertEqual(res["expired"], 1)
        self.assertLessEqual(auth[self.c["mac"]], 31 * 60)

    def test_expired_session_is_revoked(self):
        reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        clock.set_offset(61 * 60)
        res = reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        self.assertEqual(res["expired"], 1)
        self.assertEqual(self.backend.list_authorized(), {})

    def test_clock_reset_after_power_loss_fails_safe(self):
        reconcile(self.conn, self.cfg, self.backend, self.bw, {})       # heartbeat = now
        clock.set_offset(-3 * 3600)                                      # clock came back 3 h in the past
        ok, why = clock_trusted(self.conn, self.cfg)
        self.assertFalse(ok)
        state = {}
        res = reconcile(self.conn, self.cfg, self.backend, self.bw, state)
        self.assertFalse(res["clock_ok"])
        self.assertEqual(self.backend.list_authorized(), {})             # nobody gets free time
        s = self.conn.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(s["status"], "active")                          # but nothing was destroyed
        trust_clock_now(self.conn)                                       # admin confirms the date
        res = reconcile(self.conn, self.cfg, self.backend, self.bw, state)
        self.assertTrue(res["clock_ok"])

    def test_implausibly_old_clock_is_untrusted(self):
        clock.set_offset(-10 ** 9)
        self.assertFalse(clock_trusted(self.conn, self.cfg)[0])

    def test_maintenance_mode_disconnects_everyone(self):
        reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        set_setting(self.conn, "maintenance_mode", "1")
        reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        self.assertEqual(self.backend.list_authorized(), {})

    def test_blocked_client_loses_access(self):
        reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        self.conn.execute("UPDATE clients SET blocked=1 WHERE id=?", (self.c["id"],))
        reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        self.assertEqual(self.backend.list_authorized(), {})

    def test_internet_outage_does_not_touch_sessions(self):
        # internet state is informational; sessions keep counting and stay authorized
        set_setting(self.conn, "internet_status", "offline")
        res = reconcile(self.conn, self.cfg, self.backend, self.bw, {})
        self.assertEqual(res["authorized"], 1)

    def test_payment_history_survives_recovery(self):
        startup_recovery(self.conn, self.cfg, self.backend, self.bw)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT SUM(amount) FROM sales").fetchone()[0], 5)


class BackupTests(TempEnv):
    def test_backup_restore_roundtrip(self):
        c = self.make_client()
        ps.open_claim(self.conn, c["id"])
        ps.record_coin(self.conn, "b1", 5, 5, "s")
        f = backup_service.create_backup(self.cfg, "manual")
        self.assertTrue(backup_service.verify_backup(f))
        ps.open_claim(self.conn, c["id"])
        ps.record_coin(self.conn, "b2", 10, 10, "s")
        self.conn.close()
        backup_service.restore_backup(self.cfg, f, manage_services=False)
        self.conn = connect(self.cfg["data_dir"] + "/jp-pisowifi.db")
        self.assertEqual(self.conn.execute("SELECT SUM(amount) FROM sales").fetchone()[0], 5)

    def test_corrupt_backup_is_refused_and_db_untouched(self):
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(RuntimeError):
            backup_service.restore_backup(self.cfg, bad, manage_services=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 4)

    def test_retention_prunes_old_backups(self):
        self.cfg["backup"]["retention"] = 3
        for i in range(6):
            clock.set_offset(i * 10)
            backup_service.create_backup(self.cfg, "auto")
        self.assertEqual(len(backup_service.list_backups(self.cfg)), 3)


if __name__ == "__main__":
    unittest.main()
