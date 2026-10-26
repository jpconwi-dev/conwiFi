import unittest

from tests.helpers import TempEnv  # noqa: F401
from backend import bandwidth_service as bw, network_service as ns

NET = {"wan_iface": "end0", "lan_iface": "enx00e04c680001", "lan_ip": "10.0.0.1", "lan_prefix": 24,
       "dhcp_start": "10.0.0.50", "dhcp_end": "10.0.0.250", "dhcp_lease": "1h", "dns_servers": ["1.1.1.1"]}


class NetworkTests(unittest.TestCase):
    def test_valid_config_passes(self):
        self.assertEqual(ns.validate_network(NET), [])

    def test_rejects_bad_configs(self):
        self.assertTrue(ns.validate_network({**NET, "dhcp_start": "192.168.1.5"}))
        self.assertTrue(ns.validate_network({**NET, "dhcp_start": "10.0.0.1"}))       # gateway inside pool
        self.assertTrue(ns.validate_network({**NET, "lan_ip": "8.8.8.8"}))             # not private
        self.assertTrue(ns.validate_network({**NET, "lan_iface": "end0"}))             # same iface
        self.assertTrue(ns.validate_network({**NET, "dhcp_start": "10.0.0.200", "dhcp_end": "10.0.0.100"}))
        self.assertTrue(ns.validate_network({**NET, "dns_servers": ["not-an-ip"]}))

    def test_nft_ruleset_blocks_by_default_and_uses_authorized_set(self):
        r = ns.render_nft(NET)
        self.assertIn("policy drop", r)
        self.assertIn("ether saddr @authorized accept", r)
        self.assertIn("flags timeout", r)
        self.assertIn("masquerade", r)
        self.assertIn("dnat ip to 10.0.0.1:80", r)
        self.assertIn("delete table inet jp_pisowifi", r)       # reload is idempotent
        self.assertEqual(r.count("table inet jp_pisowifi {"), 1)  # no duplicated table definition

    def test_nft_only_wan_side_gets_ssh(self):
        r = ns.render_nft(NET)
        self.assertIn('iifname "end0" tcp dport { 22, 80 } accept', r)
        self.assertNotIn('iifname "enx00e04c680001" tcp dport { 22', r)

    def test_dnsmasq_has_single_range_and_gateway(self):
        d = ns.render_dnsmasq(NET)
        self.assertEqual(d.count("dhcp-range="), 1)
        self.assertIn("dhcp-option=option:router,10.0.0.1", d)
        self.assertIn("255.255.255.0", d)

    def test_interface_pick_only_when_unambiguous(self):
        a = {"name": "end0", "usb": False}; b = {"name": "enx1", "usb": True}
        self.assertEqual(ns.pick_interfaces([a, b])[:2], ("end0", "enx1"))
        self.assertEqual(ns.pick_interfaces([a])[:2], (None, None))
        self.assertEqual(ns.pick_interfaces([a, b, {"name": "enx2", "usb": True}])[:2], (None, None))

    def test_tc_commands(self):
        cmds = bw.limit_commands("eth1", 7, "10.0.0.77", 2000, 1000)
        flat = [" ".join(c) for c in cmds]
        self.assertTrue(any("htb rate 2000kbit ceil 2000kbit" in c for c in flat))
        self.assertTrue(any("match ip dst 10.0.0.77/32" in c for c in flat))
        self.assertTrue(any("dev ifb0" in c and "rate 1000kbit" in c for c in flat))
        self.assertEqual(bw.limit_commands("eth1", 7, "10.0.0.77", 0, 0), [])

    def test_bandwidth_manager_applies_only_changes(self):
        m = bw.BandwidthManager("eth1", True, None)
        m.sync({1: ("10.0.0.5", 1000, 500)})
        m.sync({1: ("10.0.0.5", 1000, 500)})
        self.assertEqual(m.applied, {1: ("10.0.0.5", 1000, 500)})
        m.sync({})
        self.assertEqual(m.applied, {})


class SimBackendTests(TempEnv):
    def test_authorize_revoke_expire(self):
        b = ns.SimBackend(self.cfg)
        b.authorize("02:00:00:00:00:01", 100)
        self.assertIn("02:00:00:00:00:01", b.list_authorized())
        b.revoke("02:00:00:00:00:01")
        self.assertEqual(b.list_authorized(), {})
        b.authorize("02:00:00:00:00:02", -5)
        self.assertEqual(b.list_authorized(), {})

    def test_network_change_rollback_flow(self):
        errs = ns.stage_network_change(self.conn, self.cfg, {"lan_ip": "10.5.0.1", "dhcp_start": "10.5.0.50",
                                                             "dhcp_end": "10.5.0.200"}, 1000)
        self.assertEqual(errs, [])
        self.assertEqual(ns.effective_network(self.conn, self.cfg)["lan_ip"], "10.5.0.1")
        self.assertIsNone(ns.rollback_due(self.conn, 1050))
        self.assertIsNotNone(ns.rollback_due(self.conn, 1000 + 500))
        ns.confirm_network_change(self.conn)
        self.assertIsNone(ns.rollback_due(self.conn, 99999))

    def test_invalid_network_change_is_rejected_and_not_stored(self):
        errs = ns.stage_network_change(self.conn, self.cfg, {"dhcp_start": "172.16.0.5"}, 1000)
        self.assertTrue(errs)
        self.assertEqual(ns.effective_network(self.conn, self.cfg)["dhcp_start"], "10.0.0.50")


if __name__ == "__main__":
    unittest.main()
