"""The aggregator: gathers every source concurrently, classifies, and returns one JSON-able dict.

Each source runs under a deadline and fails soft to an explicit state; one source raising never
blanks the others. This module is where "status" is decided, so the page can stay a dumb renderer.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from . import chat, classify, claude_limits, claude_usage, games, sources

DEADLINE_SECONDS = 6.0   # whole build; each HTTP call additionally has a 2 s timeout
HOME = os.path.expanduser("~")

# Site -> Cloudflare zone and beacon key (beacon key None = the site has no beacon). `chat: True` marks
# the one site whose pages carry the portfolio-chat widget (the chat tile lives in that site's panel).
SITES = [
    {"host": "kellogg.brengel.com", "zone": "brengel.com", "beacon": "kellogg", "chat": True},
    {"host": "rogueroutine.brengel.com", "zone": "brengel.com", "beacon": "rogueroutine"},
    {"host": "talkingrock.ai", "zone": "talkingrock.ai", "beacon": "talkingrock"},
]

# Display grouping only (never colour). Unlisted services land in "Other".
GROUPS = [
    ("Apps", ["analytics", "portfolio-chat", "flint", "stalag", "dolemite", "whoop-sync", "rogue-routine",
              "sieve", "sieve-geo", "resonance", "hermes-gateway", "bastion"]),
    ("Platform", ["forgejo", "grafana", "prometheus", "node-exporter", "portainer", "vault", "n8n", "ntfy",
                  "ollama"]),
    ("Games", ["ark-server", "ark-astraeos", "ark-genesis", "bedrock"]),   # shown in the Games panel, not Services
    ("Plumbing", ["postfix-relay", "corellia-watchers", "corellia-backup"]),
]


def _env_default(name, fallback):
    return lambda: os.environ.get(name, fallback)


@dataclass
class Config:
    prom_url: str = field(default_factory=_env_default("HUD_PROM_URL", "http://100.91.184.86:9090"))
    registry_path: str = field(default_factory=_env_default("HUD_REGISTRY", f"{HOME}/.claude/ops/registry.yaml"))
    analytics_db: str = field(default_factory=_env_default("HUD_ANALYTICS_DB", f"{HOME}/data/analytics.db"))
    product_db: str = field(default_factory=_env_default(
        "HUD_PRODUCT_DB", f"{HOME}/talking-rock/product/db/product.db"))
    cf_token_file: str = field(default_factory=_env_default(
        "HUD_CF_TOKEN_FILE", f"{HOME}/.config/corellia-hud/cloudflare.token"))
    cf_api_base: str = field(default_factory=_env_default("HUD_CF_API", "https://api.cloudflare.com/client/v4"))
    chat_dir: str = field(default_factory=_env_default("HUD_CHAT_DIR", f"{HOME}/dev/portfolio_chat/data"))
    journalctl: str = field(default_factory=_env_default("HUD_JOURNALCTL", "journalctl"))
    # Games section: Flint's loopback API, the Bedrock server-list port, and where cgroups / procs are read.
    game_flint_url: str = field(default_factory=games.default_flint_url)
    bedrock_port: int = field(default_factory=games.default_bedrock_port)
    cgroup_root: str = field(default_factory=_env_default("HUD_CGROUP_ROOT", "/sys/fs/cgroup/system.slice"))
    proc_root: str = field(default_factory=_env_default("HUD_PROC_ROOT", "/proc"))
    # Claude usage: where the transcripts live, and the Claude Code OAuth credentials file (read-only, see claude_limits).
    claude_projects_dir: str = field(default_factory=_env_default("HUD_CLAUDE_PROJECTS", f"{HOME}/.claude/projects"))
    claude_cred_path: str = field(default_factory=claude_limits.default_cred_path)


def _group_of(name):
    for group, names in GROUPS:
        if name in names:
            return group
    return "Other"


def _group_rank(name):
    order = [g for g, _ in GROUPS] + ["Other"]
    return order.index(_group_of(name))


def _collect(futs, key, default):
    """Result of a future, or `default` if it raised or missed the deadline."""
    fut = futs[key]
    try:
        return fut.result(timeout=0) if fut.done() else default
    except Exception:
        return default


def build_state(cfg, now=None, games_cache=None, limits_cache=None, usage_index=None):
    now = time.time() if now is None else now
    pool = ThreadPoolExecutor(max_workers=12)
    futs = {
        "prom": pool.submit(sources.fetch_prometheus, cfg.prom_url, now),
        "services": pool.submit(sources.load_services, cfg.registry_path),
        "health": pool.submit(sources.health_alerts, cfg.product_db, now),
        "cf": pool.submit(sources.cloudflare_stats, SITES, cfg.cf_token_file, cfg.cf_api_base, now),
    }
    if games_cache is not None:
        futs["games"] = pool.submit(games_cache.get)
    if usage_index is not None:
        futs["claude_usage"] = pool.submit(usage_index.summary, now)
    for s in SITES:
        if s["beacon"]:
            futs["beacon:" + s["host"]] = pool.submit(sources.beacon_stats, cfg.analytics_db, s["beacon"], now)
        if s.get("chat"):
            futs["chat"] = pool.submit(chat.chat_summary, cfg.chat_dir, cfg.analytics_db, now)
    wait(list(futs.values()), timeout=DEADLINE_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)

    prom = _collect(futs, "prom", {"state": "unreachable", "view": classify.PromView(ok=False, now=now),
                                   "perf": sources.empty_perf(), "alerts": []})
    reg = _collect(futs, "services", None)
    health_state, health = _collect(futs, "health", ("unreachable", []))
    cf = _collect(futs, "cf", {"state": "unreachable", "zones": [], "sites": {}})

    registry_state = "ok" if reg is not None else "missing"
    services = []
    for svc in reg or []:
        row = classify.classify_service(svc, prom["view"])
        row.update(group=_group_of(svc["name"]), port=svc["port"], kind=svc["kind"])
        services.append(row)
    services.sort(key=lambda r: (_group_rank(r["name"]), r["name"]))

    web_sites, beacon_states = [], set()
    for s in SITES:
        if s["beacon"]:
            beacon = _collect(futs, "beacon:" + s["host"], {"state": "unreachable"})
            beacon_states.add(beacon["state"])
        else:
            beacon = {"state": "no_beacon"}
        cf_site = cf["sites"].get(s["host"], {"state": cf["state"] if cf["state"] != "ok" else "unreachable"})
        entry = {"host": s["host"], "zone": s["zone"], "beacon": beacon, "cf": cf_site}
        if s.get("chat"):
            entry["chat"] = _collect(futs, "chat", {"state": "unreachable"})   # counts and metadata only
        web_sites.append(entry)
    analytics_state = next((st for st in ("unreachable", "missing") if st in beacon_states), "ok")

    perf = prom["perf"]
    alerts = sources.merge_alerts(prom["alerts"], health, [sources.disk_alert(perf["disk"]["pct"])])
    src = {"prometheus": prom["state"], "registry": registry_state, "analytics": analytics_state,
           "product_db": health_state, "cloudflare": cf["state"]}
    counts = {c: sum(1 for r in services if r["status"] == c) for c in (classify.GREEN, classify.AMBER,
                                                                         classify.RED, classify.GREY)}
    core_ok = src["prometheus"] == "ok" and registry_state == "ok" and health_state == "ok"
    all_clear = core_ok and not alerts and counts["red"] == 0 and counts["amber"] == 0
    snap = {
        "meta": {"generated_at": now, "sources": src, "all_clear": all_clear, "counts": counts,
                 "banner": _banner(src), "services_summary": classify.services_summary(services, src)},
        "alerts": alerts,
        "services": services,
        "perf": perf,
        "web": {"sites": web_sites, "cloudflare": {"state": cf["state"], "zones": cf["zones"]}},
    }
    if games_cache is not None:   # counts and metadata only: player names stay in the cache until a detail window asks
        snap["games"] = games.build_games(cfg, services, _collect(futs, "games", None), now)
    if limits_cache is not None or usage_index is not None:   # a usage KPI, not a health signal: nothing above reads it
        snap["claude"] = {
            "limits": limits_view(limits_cache.get()) if limits_cache is not None else {"state": "unavailable"},
            "usage": _collect(futs, "claude_usage", {"state": "unavailable"}) if usage_index is not None
            else {"state": "unavailable"}}
    return snap


def limits_view(snap):
    """A limits snapshot with each window's bar colour, decided in classify and only for a value that is shown."""
    out = dict(snap)
    wins = snap.get("windows")
    if wins is not None:
        shown = snap.get("state") in ("ok", "stale")
        out["windows"] = {k: dict(w, level=classify.usage_level(w["pct"]) if shown else classify.GREY)
                          for k, w in wins.items()}
    return out


def _banner(src):
    if src["prometheus"] == "unreachable":
        return "Prometheus unreachable: service and perf data unavailable"
    if src["prometheus"] == "stale":
        return "Collector stale: figures may be out of date"
    if src["registry"] != "ok":
        return "Registry unreadable: no services listed"
    if src["product_db"] != "ok":
        return "Health-check database unavailable"
    return None


class KeyedCache:
    """StateCache's shape per key: a TTL and single-flight (one builder runs per key at a time)."""

    def __init__(self, ttl, clock=time.monotonic):
        self._ttl, self._clock = ttl, clock
        self._lock = threading.Lock()
        self._slots = {}   # key -> [lock, value, at]

    def get(self, key, builder):
        with self._lock:
            slot = self._slots.setdefault(key, [threading.Lock(), None, None])
        with slot[0]:
            if slot[2] is None or self._clock() - slot[2] >= self._ttl:
                slot[1] = builder()
                slot[2] = self._clock()
            return slot[1]


class StateCache:
    """Short-TTL, single-flight cache so N page loads never mean N upstream fetches."""

    def __init__(self, builder, ttl):
        self._builder, self._ttl = builder, ttl
        self._lock = threading.Lock()
        self._value, self._at = None, 0.0

    def get(self):
        with self._lock:
            if self._value is None or time.monotonic() - self._at >= self._ttl:
                self._value = self._builder()
                self._at = time.monotonic()
            return self._value
