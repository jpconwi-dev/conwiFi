import io
import json
import re
import unittest

from tests.helpers import TempEnv
from tests.integration.test_app import AppBase
from backend import auth, clock, database, network_service as ns, payment_service as ps, session_service as ss
from backend.bandwidth_service import BandwidthManager
from backend.recovery_service import reconcile

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"0" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"0" * 32
M4A = b"\x00\x00\x00\x18ftypM4A " + b"0" * 32


class PauseServiceTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.c = self.make_client()
        ps.open_claim(self.conn, self.c["id"])
        ps.record_coin(self.conn, "p1", 5, 5, "s")            # 60 minutes

    def test_pause_freezes_time_and_resume_restores_it(self):
        clock.set_offset(20 * 60)
        self.assertEqual(ss.pause(self.conn, self.c["id"]), (True, None))
        self.assertIsNone(ss.active_session(self.conn, self.c["id"]))      # not a running session
        s = ss.current_session(self.conn, self.c["id"])
        self.assertTrue(ss.is_paused(s))
        self.assertAlmostEqual(ss.remaining_seconds(s), 40 * 60, delta=3)
        clock.set_offset(30 * 60)                                           # 10 minutes of pause pass
        self.assertAlmostEqual(ss.remaining_seconds(ss.current_session(self.conn, self.c["id"])), 40 * 60, delta=3)
        self.assertEqual(ss.resume(self.conn, self.c["id"]), (True, None))
        s = ss.active_session(self.conn, self.c["id"])
        self.assertAlmostEqual(ss.remaining_seconds(s), 40 * 60, delta=3)

    def test_cannot_pause_twice_or_when_disabled(self):
        ss.pause(self.conn, self.c["id"])
        self.assertFalse(ss.pause(self.conn, self.c["id"])[0])
        ss.resume(self.conn, self.c["id"])
        database.set_setting(self.conn, "pause_enabled", "0")
        self.assertFalse(ss.pause(self.conn, self.c["id"])[0])

    def test_pause_limit_ends_the_session(self):
        ss.pause(self.conn, self.c["id"])
        clock.set_offset(61 * 60)
        self.assertEqual(len(ss.expire_due(self.conn)), 1)
        self.assertIsNone(ss.current_session(self.conn, self.c["id"]))

    def test_resume_after_limit_fails(self):
        ss.pause(self.conn, self.c["id"])
        clock.set_offset(61 * 60)
        ok, msg = ss.resume(self.conn, self.c["id"])
        self.assertFalse(ok)
        self.assertIsNone(ss.current_session(self.conn, self.c["id"]))

    def test_coin_while_paused_adds_time_and_resumes(self):
        ss.pause(self.conn, self.c["id"])
        ps.open_claim(self.conn, self.c["id"])
        ps.record_coin(self.conn, "p2", 1, 1, "s")                          # +10 min
        s = ss.active_session(self.conn, self.c["id"])
        self.assertIsNotNone(s)
        self.assertAlmostEqual(ss.remaining_seconds(s), 70 * 60, delta=3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_paused_customer_loses_network_access_until_resume(self):
        backend, bw = ns.SimBackend(self.cfg), BandwidthManager("lan0", True, None)
        reconcile(self.conn, self.cfg, backend, bw, {})
        self.assertIn(self.c["mac"], backend.list_authorized())
        ss.pause(self.conn, self.c["id"])
        reconcile(self.conn, self.cfg, backend, bw, {})
        self.assertEqual(backend.list_authorized(), {})                      # blocked while paused
        ss.resume(self.conn, self.c["id"])
        reconcile(self.conn, self.cfg, backend, bw, {})
        self.assertIn(self.c["mac"], backend.list_authorized())              # back online

    def test_migration_3_keeps_existing_sessions(self):
        row = self.conn.execute("SELECT paused_at, paused_remaining FROM sessions").fetchone()
        self.assertEqual((row[0], row[1]), (None, None))
        self.assertEqual(database.schema_version(self.conn), 3)


class PauseApiTests(AppBase):
    def test_pause_resume_flow_through_api(self):
        self.post_json("/api/coin/start"); self.post_json("/api/sim/coin", {"value": 5})
        self.assertTrue(self.post_json("/api/pause").json["ok"])
        st = self.c.get("/api/status").json
        self.assertTrue(st["session"]["paused"])
        self.assertIsNone(st["session"]["expires_at"])
        self.assertGreater(st["session"]["pause_left"], 0)
        self.assertEqual(self.post_json("/api/pause").status_code, 400)
        self.assertTrue(self.post_json("/api/resume").json["ok"])
        st = self.c.get("/api/status").json
        self.assertFalse(st["session"]["paused"])
        self.assertAlmostEqual(st["session"]["remaining_seconds"], 3600, delta=4)

    def test_pause_without_session_is_rejected_and_csrf_required(self):
        self.assertEqual(self.post_json("/api/pause").status_code, 400)
        self.assertEqual(self.c.post("/api/pause").status_code, 400)

    def test_disconnect_ends_paused_session(self):
        self.post_json("/api/coin/start"); self.post_json("/api/sim/coin", {"value": 5})
        self.post_json("/api/pause"); self.post_json("/api/disconnect")
        self.assertIsNone(self.c.get("/api/status").json["session"])

    def test_admin_can_disable_pause(self):
        self.post_json("/api/coin/start"); self.post_json("/api/sim/coin", {"value": 5})
        c = self.login()
        t = self.token(c, "/admin/system")
        c.post("/admin/system", data={"action": "pause_settings", "max_pause": "30", "csrf_token": t})  # box unchecked
        self.assertEqual(self.post_json("/api/pause").status_code, 400)
        self.assertFalse(self.c.get("/api/status").json["pause_enabled"])

class MediaAdminTests(AppBase):
    def media_post(self, c, data, files=None):
        t = self.token(c, "/admin/media")
        payload = dict(data, csrf_token=t)
        if files:
            payload.update(files)
        return c.post("/admin/media", data=payload, content_type="multipart/form-data")

    def test_pages_render_and_require_login(self):
        self.assertEqual(self.c.get("/admin/media").status_code, 302)
        self.assertEqual(self.login().get("/admin/media").status_code, 200)

    def test_add_image_shows_on_dashboard_and_portal_by_setting(self):
        c = self.login()
        self.media_post(c, {"action": "add_image", "title": "Promo <b>", "show_on": "dashboard"},
                        {"image": (io.BytesIO(PNG), "p.png")})
        row = self.conn.execute("SELECT * FROM media").fetchone()
        self.assertTrue(row["file"].endswith(".png"))
        html = self.c.get("/").data.decode()
        self.assertIn(row["file"], html)
        self.assertNotIn("<b>", html.split("Promo")[1][:20])               # caption escaped
        self.assertEqual(html.count(row["file"]), 1)                        # dashboard only
        self.media_post(c, {"action": "update_image", "id": row["id"], "title": "x", "link": "", "show_on": "both",
                            "enabled": "on", "sort_order": "1"})
        self.assertEqual(self.c.get("/").data.decode().count(row["file"]), 2)
        self.media_post(c, {"action": "update_image", "id": row["id"], "title": "x", "link": "", "show_on": "both",
                            "sort_order": "1"})                              # 'enabled' unchecked
        self.assertNotIn(row["file"], self.c.get("/").data.decode())

    def test_bad_images_and_links_are_refused(self):
        c = self.login()
        self.media_post(c, {"action": "add_image", "title": "", "show_on": "both"},
                        {"image": (io.BytesIO(b"<svg onload=alert(1)>"), "x.png")})
        self.media_post(c, {"action": "add_image", "title": "", "show_on": "both"},
                        {"image": (io.BytesIO(PNG + b"0" * (1024 * 1024)), "big.png")})
        self.media_post(c, {"action": "add_image", "title": "", "show_on": "both", "link": "javascript:alert(1)"},
                        {"image": (io.BytesIO(PNG), "p.png")})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)

    def test_valid_link_is_rendered_safely(self):
        c = self.login()
        self.media_post(c, {"action": "add_image", "title": "", "show_on": "both", "link": "https://example.com/a"},
                        {"image": (io.BytesIO(JPG), "p.jpg")})
        html = self.c.get("/").data.decode()
        self.assertIn('href="https://example.com/a"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_delete_image_removes_row_and_file(self):
        c = self.login()
        self.media_post(c, {"action": "add_image", "title": "", "show_on": "both"}, {"image": (io.BytesIO(PNG), "p.png")})
        row = self.conn.execute("SELECT * FROM media").fetchone()
        path = self.app.config["JP_CFG"]["data_dir"] + "/uploads/" + row["file"]
        import os
        self.assertTrue(os.path.exists(path))
        self.media_post(c, {"action": "delete_image", "id": row["id"]})
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)

    def test_sound_upload_formats_and_status_urls(self):
        c = self.login()
        for ev, data, name in (("coin", MP3, "c.mp3"), ("expired", WAV, "e.wav"), ("connected", M4A, "k.m4a")):
            self.media_post(c, {"action": "sound_upload", "event": ev}, {"sound": (io.BytesIO(data), name)})
        sounds = self.c.get("/api/status").json["sounds"]
        self.assertEqual(set(sounds), {"coin", "expired", "connected"})
        r = self.c.get(sounds["coin"])
        self.assertEqual((r.status_code, r.mimetype), (200, "audio/mpeg"))
        self.assertEqual(self.c.get(sounds["connected"]).mimetype, "audio/mp4")

    def test_bad_sounds_are_refused(self):
        c = self.login()
        self.media_post(c, {"action": "sound_upload", "event": "coin"}, {"sound": (io.BytesIO(b"MZ\x90\x00" + b"0" * 50), "evil.mp3")})
        self.media_post(c, {"action": "sound_upload", "event": "coin"}, {"sound": (io.BytesIO(MP3 + b"0" * 800_000), "big.mp3")})
        self.media_post(c, {"action": "sound_upload", "event": "nope"}, {"sound": (io.BytesIO(MP3), "c.mp3")})
        self.assertEqual(self.c.get("/api/status").json["sounds"], {})

    def test_replace_and_remove_sound_cleans_old_file(self):
        import os
        c = self.login()
        self.media_post(c, {"action": "sound_upload", "event": "coin"}, {"sound": (io.BytesIO(MP3), "a.mp3")})
        first = database.get_setting(self.conn, "snd_coin")
        self.media_post(c, {"action": "sound_upload", "event": "coin"}, {"sound": (io.BytesIO(WAV), "b.wav")})
        second = database.get_setting(self.conn, "snd_coin")
        up = self.app.config["JP_CFG"]["data_dir"] + "/uploads/"
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(up + first))
        self.media_post(c, {"action": "sound_remove", "event": "coin"})
        self.assertEqual(database.get_setting(self.conn, "snd_coin"), "")
        self.assertFalse(os.path.exists(up + second))

    def test_sound_settings_and_disable(self):
        c = self.login()
        self.media_post(c, {"action": "sound_upload", "event": "coin"}, {"sound": (io.BytesIO(MP3), "a.mp3")})
        self.media_post(c, {"action": "sound_settings", "volume": "35", "low_time": "90"})   # 'play sounds' unchecked
        st = self.c.get("/api/status").json
        self.assertEqual(st["sounds"], {})
        self.assertEqual((st["sound_volume"], st["low_time_seconds"]), (0.35, 90))
        self.media_post(c, {"action": "sound_settings", "sounds_enabled": "on", "volume": "500", "low_time": "1"})
        st = self.c.get("/api/status").json
        self.assertIn("coin", st["sounds"])
        self.assertEqual((st["sound_volume"], st["low_time_seconds"]), (0.8, 60))      # out-of-range falls back to defaults

    def test_dashboard_text_is_escaped(self):
        c = self.login()
        self.media_post(c, {"action": "dash_text", "dashboard_title": "<i>Hi</i>", "dashboard_message": "<script>x</script>"})
        html = self.c.get("/").data.decode()
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;i&gt;Hi", html)

    def test_uploads_route_rejects_unknown_types_and_traversal(self):
        self.assertEqual(self.c.get("/uploads/secret_key").status_code, 404)
        self.assertEqual(self.c.get("/uploads/..%2Fjp-pisowifi.db").status_code, 404)
        self.assertEqual(self.c.get("/uploads/x.html").status_code, 404)

    def test_media_changes_are_audited(self):
        c = self.login()
        self.media_post(c, {"action": "dash_text", "dashboard_title": "T", "dashboard_message": ""})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='dashboard_text'").fetchone()[0], 1)


class ProductionModeTests(TempEnv):
    sim = False

    def test_no_dev_ui_or_simulation_endpoints_in_production(self):
        from backend import client_service
        from backend.app import create_app
        app = create_app(self.cfg)
        orig = client_service.mac_for_ip
        client_service.mac_for_ip = lambda ip: "aa:bb:cc:dd:ee:77"
        try:
            c = app.test_client()
            env = {"REMOTE_ADDR": "10.0.0.88"}
            html = c.get("/", environ_overrides=env).data.decode()
            for word in ("sim-panel", "Simulation mode", "TEST MODE", "Test coins"):
                self.assertNotIn(word, html)
            st = c.get("/api/status", environ_overrides=env).json
            self.assertFalse(st["simulation"])
            self.assertEqual(st["coin_values"], [])
            tok = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
            r = c.post("/api/sim/coin", data="{}", content_type="application/json", headers={"X-CSRF-Token": tok},
                       environ_overrides=env)
            self.assertEqual(r.status_code, 404)
        finally:
            client_service.mac_for_ip = orig

    def test_cli_sim_coin_refused_in_production(self):
        from backend import cli
        self.assertEqual(cli.main(["--config", str(self.cfg_path), "sim-coin", "5"]), 1)

    def test_branding_css_picks_readable_text_on_accent(self):
        from backend.app import _on_color
        self.assertEqual(_on_color("#c4861a"), "#111111")
        self.assertEqual(_on_color("#0b3d91"), "#ffffff")
        self.assertEqual(_on_color("#ffffff"), "#111111")


class MigrationUpgradeTests(TempEnv):
    def test_v2_database_upgrades_to_v3_without_losing_data(self):
        import sqlite3
        from pathlib import Path
        old = self.tmp / "old.db"
        conn = database.connect(old)
        base = self.tmp / "dbv2"
        (base / "migrations").mkdir(parents=True)
        (base / "schema.sql").write_text((Path(database.ROOT) / "database" / "schema.sql").read_text())
        (base / "migrations" / "002_payments_status_index.sql").write_text(
            (Path(database.ROOT) / "database" / "migrations" / "002_payments_status_index.sql").read_text())
        database.migrate(conn, base)
        self.assertEqual(database.schema_version(conn), 2)
        conn.execute("INSERT INTO clients(mac,first_seen,last_seen) VALUES('aa:aa:aa:aa:aa:aa',1,1)")
        conn.execute("INSERT INTO sessions(client_id,started_at,expires_at,purchased_seconds) VALUES(1,1,99999999999,600)")
        self.assertEqual(database.migrate(conn), [3])                      # real project migrations
        row = conn.execute("SELECT purchased_seconds, paused_at FROM sessions").fetchone()
        self.assertEqual((row[0], row[1]), (600, None))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
