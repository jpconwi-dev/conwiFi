import json
import os
import re
import stat
import unittest
from pathlib import Path

from tests.helpers import TempEnv
from tests.integration.test_app import AppBase
from backend import cli, monitor, network_service as ns, wifi_service as ws
from backend.app import create_app
from backend.bandwidth_service import BandwidthManager
from backend.database import get_setting, set_setting

IFACE = "wlx00c0caba9e6c"


class WifiRenderTests(unittest.TestCase):
    def test_open_network_2g(self):
        t = ws.render_hostapd(IFACE, "conwiFi", "", "2.4", 6, "PH", 30)
        self.assertIn(f"interface={IFACE}", t)
        self.assertIn("ssid2=" + "conwiFi".encode().hex(), t)
        self.assertIn("country_code=PH", t)
        self.assertIn("hw_mode=g", t)
        self.assertIn("channel=6", t)
        self.assertIn("ap_isolate=1", t)
        self.assertIn("max_num_sta=30", t)
        self.assertNotIn("wpa=", t)
        self.assertNotIn("wpa_passphrase", t)

    def test_wpa2_and_5ghz(self):
        t = ws.render_hostapd(IFACE, "Shop #1", "secret-pass", "5", 36, "PH", 20)
        self.assertIn("wpa=2", t)
        self.assertIn("rsn_pairwise=CCMP", t)
        self.assertIn("wpa_passphrase=secret-pass", t)
        self.assertIn("hw_mode=a", t)
        self.assertIn("ieee80211ac=1", t)

    def test_unicode_ssid_is_hex_encoded_safely(self):
        t = ws.render_hostapd(IFACE, "Caf\u00e9\nevil=1", "", "2.4", 1, "PH", 5)
        self.assertNotIn("evil=1\n", t.replace("ssid2=", ""))      # newline cannot inject a config line

    def test_validation(self):
        ok = ws.validate_wifi("conwiFi", "", "2.4", 6, 30)
        self.assertEqual(ok, [])
        self.assertTrue(ws.validate_wifi("", "", "2.4", 6, 30))
        self.assertTrue(ws.validate_wifi("x" * 33, "", "2.4", 6, 30))
        self.assertTrue(ws.validate_wifi("a\nb", "", "2.4", 6, 30))
        self.assertTrue(ws.validate_wifi("ok", "short", "2.4", 6, 30))
        self.assertTrue(ws.validate_wifi("ok", "p" * 64, "2.4", 6, 30))
        self.assertTrue(ws.validate_wifi("ok", "", "2.4", 14, 30))        # channel not allowed
        self.assertTrue(ws.validate_wifi("ok", "", "5", 52, 30))          # DFS channel refused
        self.assertTrue(ws.validate_wifi("ok", "", "6", 6, 30))
        self.assertTrue(ws.validate_wifi("ok", "", "2.4", 6, 0))
        self.assertEqual(ws.validate_wifi("ok", "goodpass1", "5", 44, 100), [])


class WifiDetectTests(TempEnv):
    def fake_sysfs(self, adapters):
        root = self.tmp / "sys_net"
        root.mkdir()
        for name, kind, usb in adapters:
            d = root / name
            d.mkdir()
            dev = self.tmp / ("usb2/2-1" if usb else "soc/ethernet") / name
            dev.mkdir(parents=True)
            (d / "device").symlink_to(dev)
            (d / "address").write_text("00:c0:ca:ba:9e:6c\n")
            (d / "operstate").write_text("down\n")
            (d / "type").write_text("1\n")
            if kind == "wifi":
                (d / "wireless").mkdir()
        return root

    def test_detects_wifi_not_ethernet_and_vice_versa(self):
        root = self.fake_sysfs([("end0", "eth", False), (IFACE, "wifi", True)])
        wifis = ns.detect_wifi_adapters(str(root))
        eths = ns.detect_interfaces(str(root))
        self.assertEqual([w["name"] for w in wifis], [IFACE])
        self.assertTrue(wifis[0]["usb"])
        self.assertEqual([e["name"] for e in eths], ["end0"])
        self.assertEqual(ns.pick_wifi_lan(eths, wifis)[:2], ("end0", IFACE))

    def test_ambiguous_wifi_layouts_are_not_guessed(self):
        eths = [{"name": "end0", "usb": False}]
        two = [{"name": "a", "usb": True}, {"name": "b", "usb": True}]
        self.assertEqual(ns.pick_wifi_lan(eths, two)[:2], (None, None))
        self.assertEqual(ns.pick_wifi_lan(eths, [])[:2], (None, None))
        mixed = [{"name": "int", "usb": False}, {"name": "ext", "usb": True}]
        self.assertEqual(ns.pick_wifi_lan(eths, mixed)[:2], ("end0", "ext"))      # prefers the USB adapter

    def test_nft_and_dnsmasq_use_the_wifi_interface(self):
        net = {**self.cfg["network"], "wan_iface": "end0", "lan_iface": IFACE, "lan_mode": "wifi"}
        self.assertEqual(ns.validate_network(net), [])
        self.assertIn(f'iifname "{IFACE}" udp dport {{ 53, 67 }} accept', ns.render_nft(net))
        self.assertIn(f"interface={IFACE}", ns.render_dnsmasq(net))
        self.assertTrue(ns.validate_network({**net, "lan_mode": "bluetooth"}))


class WifiAdminTests(AppBase):
    def setUp(self):
        TempEnv.setUp(self)
        self.cfg["network"].update({"lan_mode": "wifi", "lan_iface": IFACE, "wan_iface": "end0"})
        self.app = create_app(self.cfg)
        self.c = self.app.test_client()

    def post(self, c, data):
        t = self.token(c, "/admin/network")
        return c.post("/admin/network", data={**data, "csrf_token": t})

    def test_network_page_shows_wifi_form(self):
        html = self.login().get("/admin/network").data.decode()
        self.assertIn("WiFi access point (this Pi)", html)
        self.assertIn('name="ssid"', html)
        self.assertIn("5 GHz - channel 36", html)

    def test_save_open_network_and_request_apply(self):
        c = self.login()
        self.post(c, {"action": "wifi", "ssid": "My Shop", "open_network": "on", "chan": "2.4:11", "max_clients": "25"})
        self.assertEqual(get_setting(self.conn, "wifi_name"), "My Shop")
        self.assertEqual(get_setting(self.conn, "wifi_password"), "")
        self.assertEqual((get_setting(self.conn, "wifi_band"), get_setting(self.conn, "wifi_channel")), ("2.4", "11"))
        self.assertEqual(get_setting(self.conn, "wifi_apply_requested"), "1")
        self.assertIn(b"My Shop", self.c.get("/").data)               # portal shows the new name

    def test_password_set_kept_and_never_leaked(self):
        c = self.login()
        self.post(c, {"action": "wifi", "ssid": "conwiFi", "password": "super-secret-1", "chan": "5:40", "max_clients": "30"})
        self.assertEqual(get_setting(self.conn, "wifi_password"), "super-secret-1")
        self.post(c, {"action": "wifi", "ssid": "conwiFi2", "password": "", "chan": "5:40", "max_clients": "30"})
        self.assertEqual(get_setting(self.conn, "wifi_password"), "super-secret-1")   # blank = keep
        for page in ("/admin/network", "/admin/system", "/admin/branding", "/admin/media", "/admin/dashboard"):
            self.assertNotIn(b"super-secret-1", c.get(page).data, page)
        self.assertNotIn(b"super-secret-1", self.c.get("/").data)
        self.assertNotIn(b"super-secret-1", self.c.get("/api/status").data)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE detail LIKE '%super-secret%'").fetchone()[0], 0)

    def test_invalid_input_changes_nothing(self):
        c = self.login()
        for bad in ({"ssid": "x" * 40, "open_network": "on", "chan": "2.4:6"},
                    {"ssid": "ok", "password": "short", "chan": "2.4:6"},
                    {"ssid": "ok", "open_network": "on", "chan": "5:52"},
                    {"ssid": "ok", "open_network": "on", "chan": "garbage"},
                    {"ssid": "ok", "chan": "2.4:6"}):                          # not open and no password
            self.post(c, {"action": "wifi", "max_clients": "30", **bad})
        self.assertEqual(get_setting(self.conn, "wifi_name"), "conwiFi")
        self.assertEqual(get_setting(self.conn, "wifi_apply_requested"), "0")

    def test_branding_rename_requests_apply_in_wifi_mode(self):
        c = self.login()
        t = self.token(c, "/admin/branding")
        c.post("/admin/branding", data={"business_name": "X", "wifi_name": "Renamed", "tagline": "", "currency": "P",
                                        "instructions": "", "support_contact": "", "primary_color": "#112233",
                                        "background_color": "#000000", "text_color": "#ffffff", "csrf_token": t})
        self.assertEqual(get_setting(self.conn, "wifi_apply_requested"), "1")

    def test_monitor_consumes_apply_flag_in_simulation(self):
        set_setting(self.conn, "wifi_apply_requested", "1")
        backend, bw = ns.SimBackend(self.cfg), BandwidthManager("lan0", True, None)
        monitor.tick(self.conn, self.cfg, backend, bw, {})
        self.assertEqual(get_setting(self.conn, "wifi_apply_requested"), "0")


class WifiEthernetModeTests(AppBase):
    def test_wifi_post_refused_when_lan_is_ethernet(self):
        c = self.login()
        t = self.token(c, "/admin/network")
        c.post("/admin/network", data={"action": "wifi", "ssid": "Nope", "open_network": "on", "chan": "2.4:6",
                                       "max_clients": "30", "csrf_token": t})
        self.assertEqual(get_setting(self.conn, "wifi_name"), "conwiFi")
        self.assertNotIn(b"WiFi access point (this Pi)", c.get("/admin/network").data)


class WifiCliTests(TempEnv):
    def test_wifi_render_writes_private_file(self):
        out = self.tmp / "hostapd.conf"
        self.cfg_path.write_text(json.dumps({
            "data_dir": str(self.tmp / "data"), "simulation_mode": True,
            "network": {"lan_mode": "wifi", "lan_iface": IFACE, "wan_iface": "end0", "hostapd_conf": str(out)}}))
        self.assertEqual(cli.main(["--config", str(self.cfg_path), "wifi-render"]), 0)
        text = out.read_text()
        self.assertIn(f"interface={IFACE}", text)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)

    def test_wifi_render_refuses_on_ethernet_lan(self):
        self.assertEqual(cli.main(["--config", str(self.cfg_path), "wifi-render"]), 2)


class ShellScriptTests(unittest.TestCase):
    def test_scripts_have_valid_syntax_and_lf_endings(self):
        import subprocess
        root = Path(__file__).resolve().parents[2]
        for f in list((root / "installer").glob("*.sh")) + list((root / "scripts").glob("*.sh")) + [root / "scripts" / "jp-pisowifi"]:
            self.assertNotIn(b"\r\n", f.read_bytes(), f.name)
            self.assertEqual(subprocess.run(["bash", "-n", str(f)]).returncode, 0, f.name)
            self.assertTrue(os.access(f, os.X_OK), f"{f.name} not executable")
        for u in (root / "system" / "systemd").glob("*"):
            self.assertNotIn(b"\r\n", u.read_bytes(), u.name)


if __name__ == "__main__":
    unittest.main()
