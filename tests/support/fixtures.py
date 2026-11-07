"""Fixture builders: registry text, a beacon DB with the REAL analytics schema, a product DB.

Everything is written into a caller-supplied directory; nothing here touches the live files.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

# Copied from ~/dev/analytics (db.py create-table statement) so "against the real schema" is real.
ANALYTICS_SCHEMA = """
CREATE TABLE page_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    event_type TEXT NOT NULL,
    path TEXT NOT NULL,
    referrer TEXT,
    user_agent TEXT,
    viewport_w INTEGER,
    viewport_h INTEGER,
    screen_w INTEGER,
    screen_h INTEGER,
    time_on_page_ms INTEGER,
    scroll_depth_pct INTEGER,
    outbound_dest TEXT,
    visitor_hash TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    device_class TEXT, utm_source TEXT, utm_medium TEXT, utm_campaign TEXT);
CREATE TABLE daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL, date TEXT NOT NULL,
    pageviews INTEGER DEFAULT 0, unique_visitors INTEGER DEFAULT 0,
    avg_time_on_page_ms INTEGER, avg_scroll_depth_pct INTEGER,
    bounce_count INTEGER DEFAULT 0, outbound_clicks INTEGER DEFAULT 0,
    UNIQUE(site, date));
CREATE TABLE daily_countries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL, date TEXT NOT NULL, country TEXT NOT NULL,
    pageviews INTEGER DEFAULT 0, UNIQUE(site, date, country));
CREATE TABLE daily_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL,
    count INTEGER DEFAULT 0, UNIQUE(site, date, kind));
"""

PRODUCT_SCHEMA = """
CREATE TABLE health_check_runs (
    id INTEGER PRIMARY KEY, check_id TEXT, run_at TEXT DEFAULT (datetime('now')),
    result TEXT CHECK(result IN ('pass','fail','skip')), actual TEXT, duration_ms INTEGER);
"""

REGISTRY_UNIT = """# fixture registry
machines:
  corellia:
    role: primary-workstation

services:
  alpha:
    kind: docker
    port: 5000
    probe_host: 127.0.0.1
    status: ready
  beta:
    kind: python
    unit: beta.service
    port: 5001
    status: ready
  gamma:
    kind: systemd-user
    unit: gamma.service
    status: ready          # inline comment must be stripped
    notes: >
      folded text that mentions kind: docker and port: 9999 and must not be parsed
  zz-test:
    kind: python
    port: 5999
    status: ready
  old-thing:
    kind: docker
    port: 5002
    status: retired
  later-thing:
    kind: systemd-user
    unit: later.service
    status: deferred
  phone-thing:
    kind: android-adb
    status: ready
  game-thing:
    kind: steam-proton-game
    status: ready
  stubby:
    kind: systemd-user
    unit: stubby.service
    port: 8644
    status: stub
  rogue-ish:
    kind: hugo
    status: ready
  corellia-hud:
    kind: systemd-user
    unit: corellia-hud.service
    port: 8670
    status: ready
"""

REGISTRY_E2E_MIXED = """services:
  sieve:
    kind: python
    unit: sieve.service
    port: 5000
    status: ready
  alpha:
    kind: docker
    port: 5001
    status: ready
  loopy:
    kind: systemd-user
    unit: loopy.service
    status: ready
  ghost:
    kind: docker
    port: 5999
    status: ready
  rogue-routine:
    kind: hugo
    status: ready
"""

REGISTRY_E2E_HEALTHY = """services:
  alpha:
    kind: docker
    port: 5000
    status: ready
  beta:
    kind: docker
    port: 5001
    status: ready
  gamma:
    kind: systemd-user
    unit: gamma.service
    status: ready
"""


def utc_str(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
    return path


def make_analytics(path, events):
    """events: iterable of (site, event_type, visitor_hash, epoch[, path[, referrer]])."""
    con = sqlite3.connect(path)
    con.executescript(ANALYTICS_SCHEMA)
    for ev in events:
        site, etype, vis, epoch = ev[:4]
        page = ev[4] if len(ev) > 4 else "/"
        ref = ev[5] if len(ev) > 5 else None
        con.execute(
            "INSERT INTO page_events (site, event_type, path, referrer, visitor_hash, created_at) VALUES (?,?,?,?,?,?)",
            (site, etype, page, ref, vis, utc_str(epoch)))
    con.commit()
    con.close()
    return path


def add_rows(path, sql, rows):
    """Insert extra fixture rows into an existing fixture DB (never used on a live file)."""
    con = sqlite3.connect(path)
    con.executemany(sql, rows)
    con.commit()
    con.close()


def make_product(path, runs):
    """runs: iterable of (check_id, result, epoch)."""
    con = sqlite3.connect(path)
    con.executescript(PRODUCT_SCHEMA)
    for cid, result, epoch in runs:
        con.execute("INSERT INTO health_check_runs (check_id, run_at, result, actual) VALUES (?,?,?,?)",
                    (cid, utc_str(epoch), result, "x"))
    con.commit()
    con.close()
    return path


def beacon_events(now):
    """Known beacon values (used by unit tests and e2e scenario 6).

    kellogg: 15 visitors in the last 24 h, 10 in the 24 h before, 25 in 7 d (15 + 10 distinct), prior 7 d: 0.
    (Both 24 h windows are >= MIN_DELTA_SAMPLE so the +50% delta is a real percentage, not "was N".)
    talkingrock: 2 visitors in the last 24 h, none before; 4 visitors in 7 d.
    """
    h = 3600
    ev = []
    for v in [f"k{i}" for i in range(1, 16)]:
        ev.append(("kellogg", "pageview", v, now - 2 * h))
    ev.append(("kellogg", "pageview", "k1", now - 3 * h))      # repeat visitor, same day
    for v in [f"k{i}" for i in range(16, 26)]:
        ev.append(("kellogg", "pageview", v, now - 30 * h))     # previous 24 h window
    for v in ("t1", "t2"):
        ev.append(("talkingrock", "pageview", v, now - 5 * h))
    for v in ("t3", "t4"):
        ev.append(("talkingrock", "pageview", v, now - 4 * 24 * h))
    ev.append(("talkingrock", "pageexit", "t9", now - 1 * h))   # not a pageview: must not count
    return ev


def healthy_product_runs(now):
    return [(f"hc-{i}", "pass", now - 600) for i in range(5)]


def now():
    return time.time()


def tmpdir_path(tmp, name):
    return os.path.join(tmp, name)


# Design/e2e fixture: the shape of the real estate (names, kinds) without reading the live registry.
RICH_PORTS = {
    "analytics": 8100, "bastion": 5007, "dolemite": 8660, "flint": 8090, "hermes-gateway": 8642,
    "portfolio-chat": 8001, "resonance": 5001, "sieve": 5000, "sieve-geo": 5002, "stalag": 8650,
    "forgejo": 3000, "grafana": 3001, "n8n": 5678, "node-exporter": 9100, "ntfy": 2587,
    "ollama": 11434, "portainer": 9443, "prometheus": 9090, "vault": 8200,
}
RICH_UNITS = ["ark-server", "ark-astraeos", "ark-genesis", "bedrock", "postfix-relay", "corellia-watchers"]


def rich_registry():
    out = ["services:"]
    for name, port in RICH_PORTS.items():
        extra = "    kind: systemd-user\n    unit: stalag.service\n" if name == "stalag" else "    kind: docker\n"
        out.append(f"  {name}:\n{extra}    port: {port}\n    status: ready")
    for name in RICH_UNITS:
        out.append(f"  {name}:\n    kind: systemd\n    unit: {name}.service\n    status: ready")
    out.append("  whoop-sync:\n    kind: systemd-user\n    unit: whoop-sync.service\n    status: stub")
    out.append("  rogue-routine:\n    kind: hugo\n    status: ready")
    return "\n".join(out) + "\n"


ROGUE_PORT = 8110   # the all-green variant gives the one probe-less rich service a port, so a rich column CAN be all up


def allgreen_registry():
    """The rich registry's 27 names and groups with every probe present (rogue-routine gains a port)."""
    old = "  rogue-routine:\n    kind: hugo\n    status: ready"
    assert rich_registry().count(old) == 1
    return rich_registry().replace(old, f"  rogue-routine:\n    kind: hugo\n    port: {ROGUE_PORT}\n    status: ready")


# ---------------------------------------------------------------- detail-window fixtures
IP_HASH_SENTINEL = "IPHASHSENTINEL7"
CONTACT_EMAIL_SENTINEL = "sentinel.contact@example.invalid"
VISITOR_SENTINEL = "VISITOR-SENTINEL-A"
HOSTILE = ('<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script> '
           '<a href="javascript:window.__pwned=1">x</a> "><svg onload=window.__pwned=1>')

REGISTRY_E2E_DETAIL = """services:
  sieve:
    kind: python
    unit: sieve.service
    unit-path: /etc/systemd/system/sieve.service
    port: 5000
    status: ready
  alpha:
    kind: docker
    port: 5001
    status: ready
  portfolio-chat:
    kind: python
    unit: portfolio-chat.service
    unit-path: /etc/systemd/system/portfolio-chat.service
    port: 8001
    status: ready
  flint:
    kind: systemd-user
    unit: flint.service
    status: ready
  corellia-backup:
    kind: systemd
    unit: corellia-backup.timer
    unit-path: /etc/systemd/system/corellia-backup.timer
    status: ready
"""

# Known conversation shapes (relative to `now`): name, started offset (s), turns, response ms, blocked layer.
CHAT_CONVS = [
    ("c1", 2 * 3600, 3, 3000.0, None),
    ("c2", 30 * 3600, 2, 5000.0, None),
    ("c3", 3 * 86400, 1, 1000.0, "L3"),
    ("c4", 10 * 86400, 4, 8000.0, None),
    ("c5", 45 * 86400, 1, 700.0, None),     # outside the 30-day window
]
CHAT_UUIDS = {c[0]: f"{i:08x}-1111-4222-8333-{i:012x}" for i, c in enumerate(CHAT_CONVS, 1)}


def iso_z(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def conversation_doc(name, now, started_off, turns, resp_ms, blocked, hostile=False):
    start = now - started_off
    first = f"{VISITOR_SENTINEL} {name}" + (" " + HOSTILE if hostile and name == "c1" else "")
    msgs = []
    for t in range(turns):
        msgs.append({"role": "user", "content": first if t == 0 else f"{name} question {t + 1}",
                     "timestamp": iso_z(start + t * 60)})
        msgs.append({"role": "assistant", "content": f"{name} answer {t + 1}", "timestamp": iso_z(start + t * 60 + 30),
                     "domain": "professional", "response_time_ms": resp_ms / turns})
    return {"id": CHAT_UUIDS[name], "started_at": iso_z(start), "last_activity": iso_z(start + turns * 60),
            "ip_hash": IP_HASH_SENTINEL, "total_turns": turns, "domains_used": ["professional"],
            "total_response_time_ms": resp_ms, "blocked_at_layer": blocked, "messages": msgs}


def make_chat_tree(root, now, hostile=False):
    """A portfolio_chat data dir in the real layout (conversations/<date>/ one JSON per conversation, mode 600),
    plus contacts/ and contacts_quarantine/ whose files hold a sentinel email."""
    import json
    for name, off, turns, ms, blocked in CHAT_CONVS:
        doc = conversation_doc(name, now, off, turns, ms, blocked, hostile)
        day = os.path.join(root, "conversations", doc["started_at"][:10])
        os.makedirs(day, exist_ok=True)
        path = os.path.join(day, "conv_" + doc["id"] + ".json")
        with open(path, "w") as f:
            json.dump(doc, f)
        os.chmod(path, 0o600)
    for sub, n in (("contacts", 3), ("contacts_quarantine", 5)):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
        for i in range(n):
            path = os.path.join(root, sub, f"2026-01-22_{i:012x}.json")
            with open(path, "w") as f:
                json.dump({"message": "hello", "sender_email": CONTACT_EMAIL_SENTINEL, "sender_name": "S",
                           "ip_hash": IP_HASH_SENTINEL}, f)
            os.chmod(path, 0o600)
    return root


def detail_events(now, hostile=False):
    """Extra kellogg pageviews with paths/referrers for the site-detail view. They reuse visitors k1..k5,
    who are already in beacon_events(), so the visitor totals stay 15 / 25."""
    h = 3600
    page = "/blog/a/<img src=x onerror=window.__pwned=1>" if hostile else "/blog/a/"
    ref = "https://x.example/<script>window.__pwned=1</script>" if hostile else "https://news.ycombinator.com/"
    ev = [("kellogg", "pageview", f"k{i}", now - 5 * h, page, ref) for i in (1, 2, 3)]
    ev += [("kellogg", "pageview", f"k{i}", now - 6 * h, "/about/", "https://www.google.com/") for i in (4, 5)]
    return ev
