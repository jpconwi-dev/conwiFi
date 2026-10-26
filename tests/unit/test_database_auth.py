import unittest
from pathlib import Path

from tests.helpers import TempEnv
from backend import auth, clock, database


class MigrationTests(TempEnv):
    def test_migrations_are_idempotent_and_versioned(self):
        self.assertEqual(database.migrate(self.conn), [])
        self.assertGreaterEqual(database.schema_version(self.conn), 3)

    def test_new_migration_is_applied_once_and_data_survives(self):
        self.conn.execute("INSERT INTO clients(mac,first_seen,last_seen) VALUES('aa:aa:aa:aa:aa:aa',1,1)")
        base = self.tmp / "db"
        (base / "migrations").mkdir(parents=True)
        (base / "schema.sql").write_text((Path(database.ROOT) / "database" / "schema.sql").read_text())
        (base / "migrations" / "002_payments_status_index.sql").write_text("SELECT 1;")
        (base / "migrations" / "004_extra.sql").write_text("CREATE TABLE extra_t(id INTEGER);")
        self.assertEqual(database.migrate(self.conn, base), [4])
        self.assertEqual(database.migrate(self.conn, base), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0], 1)

    def test_failed_migration_rolls_back(self):
        base = self.tmp / "db2"
        (base / "migrations").mkdir(parents=True)
        (base / "schema.sql").write_text((Path(database.ROOT) / "database" / "schema.sql").read_text())
        (base / "migrations" / "002_payments_status_index.sql").write_text("SELECT 1;")
        (base / "migrations" / "004_bad.sql").write_text("CREATE TABLE ok_t(id INTEGER); THIS IS NOT SQL;")
        with self.assertRaises(Exception):
            database.migrate(self.conn, base)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='ok_t'").fetchone())
        self.assertEqual(database.schema_version(self.conn), 3)

    def test_seed_does_not_overwrite_or_reseed_rates(self):
        database.set_setting(self.conn, "business_name", "My Shop")
        self.conn.execute("DELETE FROM rates")
        database.seed_defaults(self.conn)
        self.assertEqual(database.get_setting(self.conn, "business_name"), "My Shop")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 0)

    def test_foreign_keys_enforced(self):
        with self.assertRaises(Exception):
            self.conn.execute("INSERT INTO sessions(client_id,started_at,expires_at) VALUES(999,1,2)")


class AuthTests(TempEnv):
    def test_password_hashing_and_verify(self):
        h = auth.hash_password("correct horse")
        self.assertNotIn("correct horse", h)
        self.assertTrue(auth.verify_password(h, "correct horse"))
        self.assertFalse(auth.verify_password(h, "wrong"))

    def test_authenticate(self):
        auth.create_admin(self.conn, "boss", "a-long-password")
        self.assertIsNotNone(auth.authenticate(self.conn, "boss", "a-long-password"))
        self.assertIsNone(auth.authenticate(self.conn, "boss", "nope"))
        self.assertIsNone(auth.authenticate(self.conn, "ghost", "x"))

    def test_rate_limit_locks_then_expires(self):
        for _ in range(5):
            auth.record_failure(self.conn, "k")
        self.assertTrue(auth.too_many_attempts(self.conn, "k", 5, 15))
        clock.set_offset(16 * 60)
        self.assertFalse(auth.too_many_attempts(self.conn, "k", 5, 15))

    def test_password_policy_and_csrf(self):
        self.assertIsNotNone(auth.validate_new_password("short"))
        self.assertIsNone(auth.validate_new_password("long-enough-1"))
        t = auth.new_csrf_token()
        self.assertTrue(auth.csrf_ok(t, t))
        self.assertFalse(auth.csrf_ok(t, "other"))
        self.assertFalse(auth.csrf_ok(None, None))


if __name__ == "__main__":
    unittest.main()
