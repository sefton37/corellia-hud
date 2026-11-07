"""Test package. The Claude defaults point at nothing, so no test can read the real credentials or the real transcripts
or reach the real usage endpoint through a default-built Config (tests that need them pass explicit paths)."""
import os

os.environ.setdefault("HUD_CLAUDE_CRED", "/nonexistent/hud-tests/fixture_cred.json")
os.environ.setdefault("HUD_CLAUDE_PROJECTS", "/nonexistent/hud-tests/projects")

# Clock shim for the midnight proofs: HUD_TEST_CLOCK_OFFSET (seconds) shifts time.time() for every test process that imports
# this package, so a suite can be run "at 00:01 local" (see tools/clock_offset.py). Unset in normal runs: no effect.
if os.environ.get("HUD_TEST_CLOCK_OFFSET"):
    import time as _time
    _real, _off = _time.time, float(os.environ["HUD_TEST_CLOCK_OFFSET"])
    _time.time = lambda: _real() + _off
