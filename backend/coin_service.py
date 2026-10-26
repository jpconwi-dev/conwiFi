"""Coin pulse handling: debouncing, grouping, pulse->value mapping, sources and the daemon loop."""
import time
import uuid

from . import clock, payment_service
from .database import log_event, set_setting


class PulseGrouper:
    """Turns raw edge timestamps into pulse groups.

    Edges closer than debounce_ms to the previous accepted edge are ignored. A group is closed when
    no accepted edge has arrived for pulse_timeout_ms. Timestamps are seconds (monotonic floats)."""

    def __init__(self, debounce_ms: int = 20, pulse_timeout_ms: int = 400):
        self.debounce = debounce_ms / 1000.0
        self.timeout = pulse_timeout_ms / 1000.0
        self.count = 0
        self.last_edge = None

    def edge(self, ts: float) -> bool:
        if self.last_edge is not None and (ts - self.last_edge) < self.debounce:
            return False
        self.count += 1
        self.last_edge = ts
        return True

    def poll(self, ts: float):
        """Return the pulse count of a finished group, or None."""
        if self.count and self.last_edge is not None and (ts - self.last_edge) >= self.timeout:
            n = self.count
            self.count, self.last_edge = 0, None
            return n
        return None


def map_pulses(pulses: int, pmap: dict):
    return pmap.get(int(pulses))


class SimulatedSource:
    """No hardware: pulses are injected with push() (used by tests and diagnostics)."""
    mode = "simulation"

    def __init__(self):
        self.pending = 0

    def push(self, n=1):
        self.pending += n

    def read_edges(self, timeout: float):
        if not self.pending:
            time.sleep(timeout)
            return []
        base = time.monotonic()
        out = [base + i * 0.05 for i in range(self.pending)]
        self.pending = 0
        return out

    def close(self):
        pass


class GpioSource:
    """libgpiod (v1 Python bindings, Debian package python3-libgpiod). NOT verified on hardware.

    Refuses to start unless coin.hardware_verified is true and gpio_line is set, so a wrong pin or
    unsafe wiring is never silently used."""
    mode = "gpio"

    def __init__(self, coin_cfg: dict):
        if not coin_cfg.get("hardware_verified"):
            raise RuntimeError("coin.hardware_verified is false. Verify the coin acceptor output and "
                               "wiring (docs/HARDWARE.md) before enabling GPIO input.")
        if coin_cfg.get("gpio_line") is None:
            raise RuntimeError("coin.gpio_line is not set (use the Linux GPIO line number, see docs).")
        try:
            import gpiod  # type: ignore
        except ImportError as e:
            raise RuntimeError("python3-libgpiod is not installed (apt install python3-libgpiod).") from e
        self.gpiod = gpiod
        self.chip = gpiod.Chip(coin_cfg["gpio_chip"])
        self.line = self.chip.get_line(int(coin_cfg["gpio_line"]))
        flags = gpiod.LINE_REQ_FLAG_ACTIVE_LOW if coin_cfg.get("active_low") else 0
        self.line.request(consumer="jp-pisowifi", type=gpiod.LINE_REQ_EV_RISING_EDGE, flags=flags)

    def read_edges(self, timeout: float):
        out = []
        if self.line.event_wait(sec=int(timeout), nsec=int((timeout % 1) * 1e9)):
            while self.line.event_wait(sec=0, nsec=0):
                self.line.event_read()
                out.append(time.monotonic())
        return out

    def close(self):
        try:
            self.line.release()
            self.chip.close()
        except Exception:  # noqa: BLE001
            pass


def process_group(conn, cfg: dict, pulses: int, source: str) -> dict:
    value = map_pulses(pulses, payment_service.pulse_map(conn))
    uid = uuid.uuid4().hex
    res = payment_service.record_coin(conn, uid, pulses, value, source,
                                      claim_seconds=int(cfg["coin"]["claim_seconds"]))
    log_event(conn, "info" if res["status"] in ("credited", "unassigned") else "warning", "coin",
              f"{pulses} pulses -> value={value} source={source} result={res['status']}")
    return res


def run_daemon(cfg: dict, conn, source=None, stop=lambda: False):
    coin_cfg = cfg["coin"]
    if source is None:
        use_sim = cfg["simulation_mode"] or (coin_cfg.get("test_mode") and not coin_cfg.get("hardware_verified"))
        source = SimulatedSource() if use_sim else GpioSource(coin_cfg)
    grouper = PulseGrouper(coin_cfg["debounce_ms"], coin_cfg["pulse_timeout_ms"])
    set_setting(conn, "coin_daemon_mode", source.mode)
    last_hb = 0.0
    while not stop():
        now = time.monotonic()
        if now - last_hb > 5:
            set_setting(conn, "coin_daemon_heartbeat", clock.now())
            last_hb = now
        for ts in source.read_edges(0.1):
            grouper.edge(ts)
        n = grouper.poll(time.monotonic())
        if n:
            process_group(conn, cfg, n, source.mode)
    source.close()
