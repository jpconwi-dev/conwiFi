"""Configuration loading. Secrets are never stored in the repository."""
import copy
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_vf = ROOT / "VERSION"
VERSION = _vf.read_text().strip() if _vf.exists() else "0.0.0"

SYSTEM_CONFIG = Path("/etc/jp-pisowifi/config.json")
LOCAL_CONFIG = ROOT / "config" / "config.local.json"

DEFAULTS = {
    "simulation_mode": True,
    "data_dir": "",
    "min_valid_epoch": 1767225600,
    "network": {
        "wan_iface": "",
        "lan_iface": "",
        "lan_mode": "ethernet",
        "wifi_country": "PH",
        "hostapd_conf": "/etc/jp-pisowifi/hostapd.conf",
        "lan_ip": "10.0.0.1",
        "lan_prefix": 24,
        "dhcp_start": "10.0.0.50",
        "dhcp_end": "10.0.0.250",
        "dhcp_lease": "1h",
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
        "confirm_seconds": 120,
    },
    "coin": {
        "gpio_chip": "gpiochip0",
        "gpio_line": None,
        "active_low": True,
        "debounce_ms": 20,
        "pulse_timeout_ms": 400,
        "claim_seconds": 30,
        "hardware_verified": False,
        "test_mode": False,
    },
    "update": {
        "repo_url": "",
        "require_signed_tag": False,
        "install_dir": "/opt/jp-pisowifi",
        "keep_releases": 3,
    },
    "backup": {"retention": 14, "interval_hours": 24},
    "security": {
        "session_minutes": 30,
        "login_max_failures": 5,
        "login_window_minutes": 15,
        "cookie_secure": False,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config_path() -> Path | None:
    env = os.environ.get("JP_PISOWIFI_CONFIG")
    if env:
        return Path(env)
    for p in (SYSTEM_CONFIG, LOCAL_CONFIG):
        if p.exists():
            return p
    return None


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    p = Path(path) if path else find_config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if p and p.exists():
        cfg = _merge(cfg, json.loads(p.read_text()))
    if overrides:
        cfg = _merge(cfg, overrides)
    data_dir = Path(cfg["data_dir"]) if cfg["data_dir"] else ROOT / "data"
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()
    cfg["data_dir"] = str(data_dir)
    cfg["_config_path"] = str(p) if p else ""
    return cfg


def paths(cfg: dict) -> dict:
    d = Path(cfg["data_dir"])
    return {
        "data": d,
        "db": d / "jp-pisowifi.db",
        "backups": d / "backups",
        "uploads": d / "uploads",
        "secret": d / "secret_key",
        "state": d / "state",
        "sim_auth": d / "state" / "sim_authorized.json",
        "sim_tc": d / "state" / "sim_tc.log",
        "update_request": d / "update.request",
        "credentials": d / "INITIAL_ADMIN_CREDENTIALS.txt",
    }


def ensure_dirs(cfg: dict) -> None:
    for key in ("data", "backups", "uploads", "state"):
        paths(cfg)[key].mkdir(parents=True, exist_ok=True)


def secret_key(cfg: dict) -> str:
    ensure_dirs(cfg)
    f = paths(cfg)["secret"]
    if not f.exists():
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return f.read_text().strip()
