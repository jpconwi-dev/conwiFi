import json
import re
import unittest

from tests.helpers import TempEnv
from backend import auth
from backend.app import create_app


class AppBase(TempEnv):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.cfg)
        self.c = self.app.test_client()

    def token(self, client=None, path="/"):
        c = client or self.c
        html = c.get(path).data.decode()
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def post_json(self, url, body=None, client=None, token=None):
        c = client or self.c
        token = token or self.token(c)
        return c.post(url, data=json.dumps(body or {}), content_type="application/json",
                      headers={"X-CSRF-Token": token})

    def login(self, user="boss", pw="a-long-password"):
        if not self.conn.execute("SELECT 1 FROM admins WHERE username=?", (user,)).fetchone():
            auth.create_admin(self.conn, user, pw)
        c = self.app.test_client()
        t = self.token(c, "/admin/login")
        r = c.post("/admin/login", data={"username": user, "password": pw, "csrf_token": t})
        self.assertEqual(r.status_code, 302, r.data[:200])
        return c


class CustomerFlowTests(AppBase):
    def test_portal_renders_rates_and_branding(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"JP-PisoWiFi", r.data)
        self.assertIn(b"\xe2\x82\xb15", r.data)
        self.assertIn(b"--accent", self.c.get("/branding.css").data)

    def test_full_coin_flow(self):
        st = self.c.get("/api/status").json
        self.assertIsNone(st["session"])
        self.assertTrue(self.post_json("/api/coin/start").json["ok"])
        res = self.post_json("/api/sim/coin", {"value": 5}).json
        self.assertEqual(res["status"], "credited")
        st = self.c.get("/api/status").json
        self.assertAlmostEqual(st["session"]["remaining_seconds"], 3600, delta=3)
        self.assertEqual(self.post_json("/api/disconnect").json["ok"], True)
        self.assertIsNone(self.c.get("/api/status").json["session"])

    def test_post_without_csrf_is_rejected(self):
        self.c.get("/")
        self.assertEqual(self.c.post("/api/coin/start").status_code, 400)
        self.assertEqual(self.c.post("/api/coin/start", headers={"X-CSRF-Token": "bad"}).status_code, 400)

    def test_second_customer_sees_busy_slot(self):
        self.assertTrue(self.post_json("/api/coin/start").json["ok"])
        other = self.app.test_client()
        other.get("/?sim=2")
        r = self.post_json("/api/coin/start", client=other)
        self.assertEqual(r.status_code, 409)

    def test_unaccepted_coin_value_rejected(self):
        self.post_json("/api/coin/start")
        self.assertEqual(self.post_json("/api/sim/coin", {"value": 3}).status_code, 400)

    def test_maintenance_blocks_new_coin_claims(self):
        from backend.database import set_setting
        set_setting(self.conn, "maintenance_mode", "1")
        self.assertEqual(self.post_json("/api/coin/start").status_code, 503)

    def test_customer_cannot_see_other_customers_data(self):
        self.post_json("/api/coin/start"); self.post_json("/api/sim/coin", {"value": 5})
        other = self.app.test_client(); other.get("/?sim=2")
        st = other.get("/api/status").json
        self.assertIsNone(st["session"])
        self.assertNotIn("mac", json.dumps(st))

    def test_security_headers(self):
        h = self.c.get("/").headers
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])

    def test_health_endpoint(self):
        self.assertEqual(self.c.get("/health").json["status"], "ok")


class AdminTests(AppBase):
    def test_admin_requires_login(self):
        for path in ("/admin/dashboard", "/admin/rates", "/admin/system", "/admin/sales.csv", "/admin/network"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/admin/login", r.headers["Location"])

    def test_wrong_password_and_lockout(self):
        auth.create_admin(self.conn, "boss", "a-long-password")
        t = self.token(path="/admin/login")
        for _ in range(5):
            r = self.c.post("/admin/login", data={"username": "boss", "password": "bad", "csrf_token": t})
            self.assertEqual(r.status_code, 401)
        r = self.c.post("/admin/login", data={"username": "boss", "password": "a-long-password", "csrf_token": t})
        self.assertEqual(r.status_code, 429)   # correct password still refused while locked

    def test_login_without_csrf_rejected(self):
        auth.create_admin(self.conn, "boss", "a-long-password")
        self.c.get("/admin/login")
        r = self.c.post("/admin/login", data={"username": "boss", "password": "a-long-password"})
        self.assertEqual(r.status_code, 400)

    def test_all_admin_pages_render(self):
        c = self.login()
        for path in ("/admin/dashboard", "/admin/rates", "/admin/sessions", "/admin/sales", "/admin/branding",
                     "/admin/network", "/admin/system", "/admin/password", "/admin/sales.csv"):
            self.assertEqual(c.get(path).status_code, 200, path)

    def test_forced_password_change(self):
        auth.create_admin(self.conn, "new", "initial-pass-1", must_change=True)
        c = self.login("new", "initial-pass-1")
        r = c.get("/admin/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/password", r.headers["Location"])
        t = self.token(c, "/admin/password")
        r = c.post("/admin/password", data={"current": "initial-pass-1", "new": "brand-new-pass", "confirm": "brand-new-pass", "csrf_token": t})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/admin/dashboard").status_code, 200)

    def test_rate_edit_changes_portal_and_is_audited(self):
        c = self.login()
        t = self.token(c, "/admin/rates")
        c.post("/admin/rates", data={"action": "add", "amount": "50", "minutes": "999", "csrf_token": t})
        self.assertIn(b"\xe2\x82\xb150", self.c.get("/").data)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='rate_add'").fetchone()[0], 1)

    def test_rate_validation(self):
        c = self.login()
        t = self.token(c, "/admin/rates")
        c.post("/admin/rates", data={"action": "add", "amount": "-4", "minutes": "x", "csrf_token": t})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0], 4)

    def test_xss_in_business_name_is_escaped(self):
        c = self.login()
        t = self.token(c, "/admin/branding")
        c.post("/admin/branding", data={"business_name": "<script>alert(1)</script>", "tagline": "", "currency": "P",
                                        "instructions": "", "support_contact": "", "wifi_name": "conwiFi", "primary_color": "#112233",
                                        "background_color": "#000000", "text_color": "#ffffff", "csrf_token": t})
        html = self.c.get("/").data.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_wifi_name_default_editable_and_validated(self):
        self.assertIn(b"conwiFi", self.c.get("/").data)
        c = self.login()
        t = self.token(c, "/admin/branding")
        base = {"business_name": "X", "tagline": "", "currency": "P", "instructions": "", "support_contact": "",
                "wifi_name": "My New WiFi", "primary_color": "#112233", "background_color": "#000000",
                "text_color": "#ffffff", "csrf_token": t}
        c.post("/admin/branding", data=base)
        self.assertIn(b"My New WiFi", self.c.get("/").data)
        base["wifi_name"] = "x" * 33
        c.post("/admin/branding", data=base)
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='wifi_name'").fetchone()[0], "My New WiFi")

    def test_bad_color_and_bad_logo_rejected(self):
        import io
        c = self.login()
        t = self.token(c, "/admin/branding")
        base = {"business_name": "X", "tagline": "", "currency": "P", "instructions": "", "support_contact": "",
                "wifi_name": "conwiFi", "primary_color": "red;}body{display:none", "background_color": "#000000", "text_color": "#ffffff", "csrf_token": t}
        c.post("/admin/branding", data=base)
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='business_name'").fetchone()[0], "JP-PisoWiFi")
        base["primary_color"] = "#112233"
        base["logo"] = (io.BytesIO(b"<svg onload=alert(1)>"), "x.png")
        c.post("/admin/branding", data=base, content_type="multipart/form-data")
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='logo_file'").fetchone()[0], "")

    def test_valid_png_logo_accepted(self):
        import io
        c = self.login()
        t = self.token(c, "/admin/branding")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
        c.post("/admin/branding", data={"business_name": "X", "tagline": "", "currency": "P", "instructions": "",
                                        "support_contact": "", "wifi_name": "conwiFi", "primary_color": "#112233", "background_color": "#000000",
                                        "text_color": "#ffffff", "csrf_token": t, "logo": (io.BytesIO(png), "l.png")},
               content_type="multipart/form-data")
        self.assertTrue(self.conn.execute("SELECT value FROM settings WHERE key='logo_file'").fetchone()[0].endswith(".png"))

    def test_admin_can_disconnect_session(self):
        self.post_json("/api/coin/start"); self.post_json("/api/sim/coin", {"value": 5})
        c = self.login()
        sid = self.conn.execute("SELECT id FROM sessions").fetchone()[0]
        t = self.token(c, "/admin/sessions")
        c.post("/admin/sessions", data={"action": "disconnect", "id": sid, "csrf_token": t})
        self.assertEqual(self.conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()[0], "ended")

    def test_backup_path_traversal_blocked(self):
        c = self.login()
        self.assertEqual(c.get("/admin/backup/..%2F..%2Fetc%2Fpasswd").status_code, 404)
        self.assertEqual(c.get("/admin/backup/evil.db").status_code, 404)

    def test_sales_csv_and_totals(self):
        self.post_json("/api/coin/start"); self.post_json("/api/sim/coin", {"value": 10})
        c = self.login()
        csv_text = c.get("/admin/sales.csv").data.decode()
        self.assertIn("credited", csv_text)
        self.assertIn(b"10", c.get("/admin/dashboard").data)

    def test_logout_ends_session(self):
        c = self.login()
        t = self.token(c, "/admin/dashboard")
        c.post("/admin/logout", data={"csrf_token": t})
        self.assertEqual(c.get("/admin/dashboard").status_code, 302)


class NonSimGuardTests(TempEnv):
    sim = False

    def test_admin_hidden_from_unlisted_lan_clients(self):
        from backend import client_service
        app = create_app(self.cfg)
        orig = client_service.mac_for_ip
        client_service.mac_for_ip = lambda ip: "aa:bb:cc:dd:ee:01"
        try:
            c = app.test_client()
            # customer on the LAN subnet
            r = c.get("/admin/login", environ_overrides={"REMOTE_ADDR": "10.0.0.77"})
            self.assertEqual(r.status_code, 404)
            # management side (not in LAN subnet) is allowed
            r = c.get("/admin/login", environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
            self.assertEqual(r.status_code, 200)
            # allow-listed customer-side MAC
            from backend.database import set_setting
            set_setting(self.conn, "admin_lan_macs", json.dumps(["aa:bb:cc:dd:ee:01"]))
            r = c.get("/admin/login", environ_overrides={"REMOTE_ADDR": "10.0.0.77"})
            self.assertEqual(r.status_code, 200)
        finally:
            client_service.mac_for_ip = orig

    def test_test_mode_enables_sim_coins_with_real_networking(self):
        from backend import client_service
        self.cfg["coin"]["test_mode"] = True
        app = create_app(self.cfg)
        orig = client_service.mac_for_ip
        client_service.mac_for_ip = lambda ip: "aa:bb:cc:dd:ee:09"
        try:
            c = app.test_client()
            env = {"REMOTE_ADDR": "10.0.0.77"}
            html = c.get("/", environ_overrides=env).data.decode()
            self.assertIn("TEST MODE", html)
            tok = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
            hdr = {"X-CSRF-Token": tok}
            self.assertEqual(c.get("/api/status", environ_overrides=env).json["simulation"], True)
            self.assertEqual(c.post("/api/coin/start", headers=hdr, environ_overrides=env).status_code, 200)
            r = c.post("/api/sim/coin", data=json.dumps({"value": 5}), content_type="application/json",
                       headers=hdr, environ_overrides=env)
            self.assertEqual(r.json["status"], "credited")
        finally:
            client_service.mac_for_ip = orig

    def test_sim_coin_route_is_closed_without_test_mode(self):
        app = create_app(self.cfg)
        c = app.test_client()
        html = c.get("/", environ_overrides={"REMOTE_ADDR": "10.0.0.77"}).data.decode()
        tok = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        r = c.post("/api/sim/coin", data="{}", content_type="application/json",
                   headers={"X-CSRF-Token": tok}, environ_overrides={"REMOTE_ADDR": "10.0.0.77"})
        self.assertEqual(r.status_code, 404)

    def test_foreign_host_redirects_to_portal(self):
        app = create_app(self.cfg)
        r = app.test_client().get("/generate_204", headers={"Host": "connectivitycheck.gstatic.com"},
                                  environ_overrides={"REMOTE_ADDR": "10.0.0.77"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "http://10.0.0.1/")


if __name__ == "__main__":
    unittest.main()
