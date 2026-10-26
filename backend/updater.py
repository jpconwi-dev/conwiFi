"""Release-based updater: clone a tagged release into releases/<tag>, migrate, switch the `current`
symlink, restart, health-check, and switch back on failure. Never touches config or the database
files except through additive migrations."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import backup_service, clock
from .config import VERSION
from .database import connect, get_setting, log_event, migrate, set_setting
from .config import paths

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SERVICES = ["jp-pisowifi", "jp-pisowifi-monitor", "jp-pisowifi-coin"]


class UpdateError(RuntimeError):
    pass


def semver(tag: str):
    m = TAG_RE.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def check_repo_url(url: str, allow_local: bool = False):
    if not url or "YOUR_USERNAME" in url:
        raise UpdateError("update.repo_url is not configured in /etc/jp-pisowifi/config.json")
    if url.startswith("https://") or (allow_local and (url.startswith("file://") or url.startswith("/"))):
        return
    raise UpdateError("Only https:// repositories are accepted for updates")


def latest_tag(tags: list[str]) -> str | None:
    valid = [t for t in tags if semver(t)]
    return max(valid, key=semver) if valid else None


def remote_tags(url: str) -> list[str]:
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", url], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise UpdateError("Cannot reach repository: " + r.stderr.strip()[:200])
    return [line.split("refs/tags/")[-1] for line in r.stdout.splitlines() if "refs/tags/" in line]


def switch_symlink(link: Path, target: Path):
    tmp = link.with_name(link.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)  # atomic


def run_update(cfg: dict, target: str | None = None, *, allow_local=False, manage_services=True,
               pip_install=True, health_cmd=True, venv_python: str | None = None) -> str:
    ucfg = cfg["update"]
    install = Path(ucfg["install_dir"])
    url = ucfg["repo_url"]
    check_repo_url(url, allow_local)
    conn = connect(paths(cfg)["db"])
    py = venv_python or str(install / "venv" / "bin" / "python")
    try:
        tags = remote_tags(url)
        tag = target or latest_tag(tags)
        if not tag or not semver(tag) or tag not in tags:
            raise UpdateError(f"No valid release tag found (wanted: {target or 'latest'})")
        if semver(tag) == semver("v" + VERSION):
            return f"Already running {tag}"
        backup = backup_service.create_backup(cfg, "pre-update")
        releases = install / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        new_dir = releases / tag
        if new_dir.exists():
            shutil.rmtree(new_dir)
        r = subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", tag, url, str(new_dir)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise UpdateError("Download failed: " + r.stderr.strip()[:200])
        if ucfg.get("require_signed_tag"):
            v = subprocess.run(["git", "-C", str(new_dir), "verify-tag", tag], capture_output=True, text=True)
            if v.returncode != 0:
                shutil.rmtree(new_dir, ignore_errors=True)
                raise UpdateError("Tag signature verification failed; update refused")
        if not (new_dir / "backend" / "app.py").exists():
            shutil.rmtree(new_dir, ignore_errors=True)
            raise UpdateError("Downloaded release does not look like JP-PisoWiFi")
        shutil.rmtree(new_dir / ".git", ignore_errors=True)
        if pip_install:
            p = subprocess.run([py, "-m", "pip", "install", "--quiet", "-r", str(new_dir / "requirements.txt")],
                               capture_output=True, text=True)
            if p.returncode != 0:
                raise UpdateError("Dependency install failed: " + p.stderr.strip()[-200:])
        # migrations are additive (see docs/UPDATING.md) so the old release keeps working on the new schema
        m = subprocess.run([py, "-m", "backend.cli", "migrate"], cwd=new_dir, capture_output=True, text=True,
                           env={**os.environ, "JP_PISOWIFI_CONFIG": cfg.get("_config_path", "")})
        if m.returncode != 0:
            raise UpdateError("Migration failed: " + (m.stderr or m.stdout).strip()[-200:])
        current = install / "current"
        previous = current.resolve() if current.exists() else None
        switch_symlink(current, new_dir)
        if manage_services:
            subprocess.run(["systemctl", "restart", *SERVICES], capture_output=True)
        healthy = True
        if health_cmd and manage_services:
            import time
            time.sleep(8)
            healthy = subprocess.run([py, "-m", "backend.cli", "health", "--quiet"], cwd=current,
                                     env={**os.environ, "JP_PISOWIFI_CONFIG": cfg.get("_config_path", "")}
                                     ).returncode == 0
        if not healthy:
            if previous:
                switch_symlink(current, previous)
                if manage_services:
                    subprocess.run(["systemctl", "restart", *SERVICES], capture_output=True)
            raise UpdateError(f"Health check failed after update; rolled back to {previous.name if previous else '?'}"
                              f" (database backup: {backup.name})")
        _prune_releases(releases, current, int(ucfg.get("keep_releases", 3)))
        msg = f"Updated to {tag} (backup: {backup.name})"
        set_setting(conn, "last_update_result", f"{clock.now()}|ok|{msg}")
        log_event(conn, "info", "update", msg)
        return msg
    except UpdateError as e:
        set_setting(conn, "last_update_result", f"{clock.now()}|failed|{e}")
        log_event(conn, "error", "update", str(e))
        raise
    finally:
        conn.close()


def _prune_releases(releases: Path, current: Path, keep: int):
    cur = current.resolve()
    dirs = sorted([d for d in releases.iterdir() if d.is_dir() and semver(d.name)], key=lambda d: semver(d.name))
    for d in dirs[:-keep]:
        if d.resolve() != cur:
            shutil.rmtree(d, ignore_errors=True)
