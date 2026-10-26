"""Consistent SQLite backups (online backup API), retention and restore."""
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import clock
from .config import paths
from .database import connect, integrity_ok, migrate, set_setting

SERVICES = ["jp-pisowifi", "jp-pisowifi-monitor", "jp-pisowifi-coin"]


def create_backup(cfg: dict, label: str = "auto") -> Path:
    p = paths(cfg)
    p["backups"].mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock.now()))
    dest = p["backups"] / f"jp-pisowifi-{label}-{stamp}.db"
    src = sqlite3.connect(str(p["db"]))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # safe even while WAL is active; a plain file copy is not
    finally:
        dst.close()
        src.close()
    if cfg.get("_config_path") and Path(cfg["_config_path"]).exists():
        shutil.copy2(cfg["_config_path"], str(dest) + ".config.json")
    prune(cfg)
    return dest


def prune(cfg: dict):
    keep = int(cfg["backup"]["retention"])
    files = sorted(paths(cfg)["backups"].glob("jp-pisowifi-*.db"))
    pre = [f for f in files if "-pre-" in f.name]  # pre-update / pre-restore copies are kept longer
    normal = [f for f in files if f not in pre]
    for old in normal[:-keep] + pre[:-max(keep, 5)]:
        old.unlink(missing_ok=True)
        Path(str(old) + ".config.json").unlink(missing_ok=True)


def list_backups(cfg: dict):
    return sorted(paths(cfg)["backups"].glob("jp-pisowifi-*.db"), reverse=True)


def verify_backup(path: Path) -> bool:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.close()
        return ok
    except sqlite3.Error:
        return False


def restore_backup(cfg: dict, backup: Path, manage_services: bool = True) -> Path:
    backup = Path(backup)
    if not backup.exists() or not verify_backup(backup):
        raise RuntimeError("Backup file is missing or failed the integrity check; nothing was changed.")
    pre = create_backup(cfg, "pre-restore")
    if manage_services:
        subprocess.run(["systemctl", "stop", *SERVICES], capture_output=True)
    p = paths(cfg)
    for suffix in ("", "-wal", "-shm"):
        Path(str(p["db"]) + suffix).unlink(missing_ok=True)
    shutil.copy2(backup, p["db"])
    conn = connect(p["db"])
    migrate(conn)  # bring an older backup up to the current schema
    ok = integrity_ok(conn)
    set_setting(conn, "last_heartbeat", clock.now())
    conn.close()
    if manage_services:
        subprocess.run(["systemctl", "start", *SERVICES], capture_output=True)
    if not ok:
        raise RuntimeError(f"Restored database failed integrity check. Previous copy: {pre}")
    return pre
