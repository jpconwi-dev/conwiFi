"""Single time source so tests can simulate reboots and clock problems."""
import time

_offset = 0


def now() -> int:
    return int(time.time()) + _offset


def set_offset(seconds: int) -> None:
    global _offset
    _offset = int(seconds)


def reset() -> None:
    set_offset(0)


def local_date(ts: int | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now() if ts is None else ts))
