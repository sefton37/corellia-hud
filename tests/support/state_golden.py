"""Deterministic build_state inputs for the failsoft scenarios, and the golden snapshot taken from them
BEFORE meta.services_summary existed (Spec #492). tests/test_services_compact.py compares every other key
of today's output to this file, byte for byte, so the additive summary cannot have moved anything else.

CLI: python3 -m tests.support.state_golden --write   (rewrites tests/golden/state_failsoft.json from the CURRENT code;
only meaningful on a tree where meta.services_summary has been removed)."""
import json
import os
import sys
import tempfile

from corellia_hud import state
from tests.support import fixtures as fx
from tests.support.fake_prom import FakeProm, healthy_data

NOW = 1_800_000_000.0
DEAD = "http://127.0.0.1:9"
GOLDEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "golden", "state_failsoft.json")
SCENARIOS = ("prom_down", "registry_missing", "healthy")


def build(scenario):
    with tempfile.TemporaryDirectory() as t:
        reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_HEALTHY)
        adb = fx.make_analytics(os.path.join(t, "a.db"), fx.beacon_events(NOW))
        pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(NOW))
        fake = FakeProm(healthy_data()).start() if scenario == "healthy" else None
        try:
            cfg = state.Config(prom_url=fake.url if fake else DEAD,
                               registry_path="/nonexistent/registry.yaml" if scenario == "registry_missing" else reg,
                               analytics_db=adb, product_db=pdb, cf_token_file=os.path.join(t, "no-such-token"),
                               cf_api_base=DEAD)
            return state.build_state(cfg, now=NOW)
        finally:
            if fake:
                fake.stop()


def comparable(snap):
    """The snapshot with the one additive key removed and the volatile (wall-clock) fields dropped."""
    out = json.loads(json.dumps(snap))
    out["meta"].pop("services_summary", None)
    return out


if __name__ == "__main__":
    if "--write" in sys.argv:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, "w") as f:
            json.dump({s: comparable(build(s)) for s in SCENARIOS}, f, indent=1, sort_keys=True)
        print(GOLDEN)
