import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import ROOT, TempEnv
from backend import updater


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": "/tmp"})


@unittest.skipUnless(shutil.which("git"), "git not installed")
class UpdaterTests(TempEnv):
    def make_repo(self, versions=("9.0.0",), broken=()):
        repo = self.tmp / "remote"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        for v in versions:
            for item in ("backend", "database", "frontend", "requirements.txt"):
                src, dst = ROOT / item, repo / item
                if dst.exists():
                    shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__")) if src.is_dir() else shutil.copy(src, dst)
            (repo / "VERSION").write_text(v + "\n")
            if v in broken:
                (repo / "backend" / "app.py").unlink()
            git("add", "-A", cwd=repo)
            git("commit", "-q", "-m", f"release {v}", cwd=repo)
            git("tag", f"v{v}", cwd=repo)
        self.cfg["update"]["repo_url"] = str(repo)
        return repo

    def run_update(self, target=None):
        return updater.run_update(self.cfg, target, allow_local=True, manage_services=False, pip_install=False,
                                  health_cmd=False, venv_python=sys.executable)

    def test_tag_selection_and_validation(self):
        self.assertEqual(updater.latest_tag(["v1.2.0", "v1.10.0", "v1.9.9", "nightly", "v2"]), "v1.10.0")
        self.assertIsNone(updater.latest_tag(["main", "foo"]))
        with self.assertRaises(updater.UpdateError):
            updater.check_repo_url("http://example.com/x.git")
        with self.assertRaises(updater.UpdateError):
            updater.check_repo_url("https://github.com/YOUR_USERNAME/JP-PisoWiFi.git")
        with self.assertRaises(updater.UpdateError):
            updater.check_repo_url("")
        updater.check_repo_url("https://github.com/someone/JP-PisoWiFi.git")

    def test_update_installs_release_backs_up_and_keeps_data(self):
        self.make_repo(("9.0.0",))
        self.conn.execute("INSERT INTO clients(mac,first_seen,last_seen) VALUES('aa:aa:aa:aa:aa:aa',1,1)")
        msg = self.run_update()
        self.assertIn("v9.0.0", msg)
        cur = Path(self.cfg["update"]["install_dir"]) / "current"
        self.assertTrue(cur.is_symlink())
        self.assertTrue((cur.resolve() / "backend" / "app.py").exists())
        self.assertFalse((cur.resolve() / ".git").exists())
        self.assertTrue(list(Path(self.cfg["data_dir"], "backups").glob("*pre-update*.db")))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0], 1)
        self.assertIn("|ok|", self.conn.execute("SELECT value FROM settings WHERE key='last_update_result'").fetchone()[0])

    def test_picks_latest_and_second_run_is_noop_for_same_version(self):
        self.make_repo(("9.0.0", "9.1.0"))
        self.assertIn("v9.1.0", self.run_update())
        self.assertIn("v9.0.0", self.run_update("v9.0.0"))   # explicit older tag allowed (manual rollback)

    def test_broken_release_is_refused_and_current_unchanged(self):
        self.make_repo(("9.0.0", "9.2.0"), broken=("9.2.0",))
        self.run_update("v9.0.0")
        cur = Path(self.cfg["update"]["install_dir"]) / "current"
        before = cur.resolve()
        with self.assertRaises(updater.UpdateError):
            self.run_update("v9.2.0")
        self.assertEqual(cur.resolve(), before)
        self.assertIn("|failed|", self.conn.execute("SELECT value FROM settings WHERE key='last_update_result'").fetchone()[0])

    def test_unknown_tag_refused(self):
        self.make_repo(("9.0.0",))
        with self.assertRaises(updater.UpdateError):
            self.run_update("v1.2.3")

    def test_symlink_switch_is_atomic_and_reversible(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir(); b.mkdir()
        link = self.tmp / "current"
        updater.switch_symlink(link, a)
        self.assertEqual(link.resolve(), a.resolve())
        updater.switch_symlink(link, b)
        self.assertEqual(link.resolve(), b.resolve())


if __name__ == "__main__":
    unittest.main()
