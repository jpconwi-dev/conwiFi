import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import clock  # noqa: E402
from backend.config import load_config  # noqa: E402
from backend.database import init_db  # noqa: E402


class TempEnv(unittest.TestCase):
    """Fresh data dir + database per test, clock reset."""

    sim = True

    def setUp(self):
        clock.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix="jpt-"))
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({"data_dir": str(self.tmp / "data"), "simulation_mode": self.sim,
                                             "update": {"install_dir": str(self.tmp / "install")}}))
        self.cfg = load_config(self.cfg_path)
        self.conn = init_db(self.cfg)

    def tearDown(self):
        clock.reset()
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_client(self, n=1):
        from backend import client_service
        mac, ip = client_service.sim_identity(n)
        return client_service.upsert_client(self.conn, mac, ip)
