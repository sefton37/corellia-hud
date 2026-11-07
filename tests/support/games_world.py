"""A whole fake world for the Games section, in-process: fake Prometheus, fake Flint, a fake Bedrock UDP
responder, a fake journalctl serving a Bedrock journal file, and the cgroup / proc / save / registry trees.
Used by tests/test_games.py and tests/test_games_state.py. close() releases every port and env var."""
import os
import tempfile
import time

from corellia_hud import state
from tests.support import games_env as ge
from tests.support.fake_bedrock import FakeBedrock
from tests.support.fake_flint import FakeFlint
from tests.support.fake_prom import FakeProm, healthy_data

FAKE_JOURNALCTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_journalctl.py")
UNITS = [s["unit"] for s in ge.SERVERS.values()]


def prom_data(active=None, failed=None, loops=None, restarts24=1):
    """Prometheus rows for the four game units (scope system). `active` / `failed` map unit -> 0|1."""
    active, failed, loops = active or {}, failed or {}, loops or {}
    d = healthy_data()
    d["unit_active"] = [({"scope": "system", "unit": u}, active.get(u, 1)) for u in UNITS]
    d["unit_failed"] = [({"scope": "system", "unit": u}, failed.get(u, 0)) for u in UNITS]
    d["restarts"] = [({"scope": "system", "unit": u}, loops.get(u, 0)) for u in UNITS]
    d["restarts24h"] = [({"scope": "system", "unit": u}, restarts24) for u in UNITS]
    return d


class World:
    def __init__(self, stalag="absent", ping_mode="ok", players=(), ping_players=0, hostile=False, flint_cfg=None,
                 prom=None, journal_players=(ge.BED_NAME,), now=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.now = time.time() if now is None else now
        ge.make_all(self.root, self.now)
        self.registry = ge.write(os.path.join(self.root, "registry.yaml"), ge.registry_text(self.root, stalag))
        self.journal_file = ge.write(os.path.join(self.root, "bedrock.log"),
                                     ge.bedrock_journal(self.now, hostile, journal_players))
        self.journal_log = os.path.join(self.root, "journal-calls.log")
        os.environ["FAKE_JOURNAL_FILE"] = self.journal_file
        os.environ["FAKE_JOURNAL_LOG"] = self.journal_log
        self.flint = FakeFlint(flint_cfg if flint_cfg is not None else ge.flint_config(self.now, players, hostile)).start()
        self.bedrock = FakeBedrock(ping_mode, players=ping_players).start()
        self.prom = FakeProm(prom if prom is not None else prom_data(), port=0).start()
        self.cfg = state.Config(
            prom_url=self.prom.url, registry_path=self.registry, analytics_db=os.path.join(self.root, "none.db"),
            product_db=os.path.join(self.root, "none2.db"), cf_token_file=os.path.join(self.root, "none"),
            cf_api_base="http://127.0.0.1:9", chat_dir=os.path.join(self.root, "chat"),
            journalctl=FAKE_JOURNALCTL, game_flint_url=self.flint.url, bedrock_port=self.bedrock.port,
            cgroup_root=os.path.join(self.root, "cgroup"), proc_root=os.path.join(self.root, "proc"))

    def journal_calls(self):
        if not os.path.exists(self.journal_log):
            return 0
        with open(self.journal_log) as f:
            return len(f.readlines())

    def close(self):
        self.flint.stop()
        self.bedrock.stop()
        self.prom.stop()
        os.environ.pop("FAKE_JOURNAL_FILE", None)
        os.environ.pop("FAKE_JOURNAL_LOG", None)
        self.tmp.cleanup()
