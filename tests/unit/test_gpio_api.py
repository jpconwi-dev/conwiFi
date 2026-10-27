"""GpioSource against fake libgpiod modules (v1 and v2 APIs). This checks our code paths only,
not real GPIO behaviour."""
import sys
import types
import unittest

from tests.helpers import ROOT  # noqa: F401
from backend.coin_service import GpioSource

CFG = {"gpio_chip": "gpiochip0", "gpio_line": 12, "active_low": True, "hardware_verified": True}


def fake_v2(events):
    m = types.ModuleType("gpiod")
    line = types.ModuleType("gpiod.line")
    line.Direction = types.SimpleNamespace(INPUT="in")
    line.Edge = types.SimpleNamespace(RISING="rising")

    class LineSettings:
        def __init__(self, **kw):
            self.kw = kw

    class Req:
        def __init__(self, path, consumer, config):
            self.path, self.config, self.released, self.events = path, config, False, list(events)

        def wait_edge_events(self, timeout):
            return bool(self.events)

        def read_edge_events(self):
            ev, self.events = self.events, []
            return ev

        def release(self):
            self.released = True

    m.LineSettings = LineSettings
    m.request_lines = lambda path, consumer, config: Req(path, consumer, config)
    m.line = line
    return m, line


def fake_v1(n):
    m = types.ModuleType("gpiod")
    m.LINE_REQ_FLAG_ACTIVE_LOW, m.LINE_REQ_EV_RISING_EDGE = 1, 2

    class Line:
        def __init__(self):
            self.pending = n

        def request(self, **kw):
            self.kw = kw

        def event_wait(self, sec=0, nsec=0):
            return self.pending > 0

        def event_read(self):
            self.pending -= 1

        def release(self):
            pass

    class Chip:
        def __init__(self, name):
            self.l = Line()

        def get_line(self, n):
            return self.l

        def close(self):
            pass

    m.Chip = Chip
    return m


class GpioApiTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("gpiod", None)
        sys.modules.pop("gpiod.line", None)

    def test_refuses_without_verification_or_line(self):
        with self.assertRaises(RuntimeError):
            GpioSource({**CFG, "hardware_verified": False})
        with self.assertRaises(RuntimeError):
            GpioSource({**CFG, "gpio_line": None})

    def test_v2_api(self):
        m, line = fake_v2([object(), object(), object()])
        sys.modules["gpiod"], sys.modules["gpiod.line"] = m, line
        g = GpioSource(CFG)
        self.assertTrue(g.v2)
        self.assertEqual(g.request.path, "/dev/gpiochip0")
        self.assertEqual(g.request.config[12].kw["active_low"], True)
        self.assertEqual(len(g.read_edges(0.1)), 3)
        self.assertEqual(g.read_edges(0.1), [])
        g.close()
        self.assertTrue(g.request.released)

    def test_v1_api(self):
        sys.modules["gpiod"] = fake_v1(2)
        g = GpioSource(CFG)
        self.assertFalse(g.v2)
        self.assertEqual(len(g.read_edges(0.1)), 2)
        self.assertEqual(g.read_edges(0.1), [])

    def test_missing_library_gives_clear_error(self):
        sys.modules["gpiod"] = None  # makes `import gpiod` raise ImportError
        with self.assertRaises(RuntimeError):
            GpioSource(CFG)


if __name__ == "__main__":
    unittest.main()
