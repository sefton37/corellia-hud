"""GET /api/detail/<kind>?id=<id>: the data behind every stat in the HUD.

The allowlist is derived here, from the aggregator's own state, never from the request: `kind` must be
one of KINDS and `id` must be an exact member of the set the aggregator currently knows for that kind
(registry names, the four statuses, the perf tiles, the site hosts, the chat host, firing alert names,
the game-server table).
Everything returned is explicit-state JSON; a missing source is a state, never a zero. Nothing is logged.
"""
import math
import re
import time

from . import chat, claude_usage, classify, games, journal, sources, state as hud_state

KINDS = ("service", "services", "perf", "site", "chat", "alert", "game", "claude")
CLAUDE_IDS = ("limits",) + claude_usage.DETAIL_IDS
PERF_IDS = ("cpu", "mem", "gpu", "vram", "disk", "load", "uptime")
STATUSES = (classify.GREEN, classify.AMBER, classify.RED, classify.GREY)
# The one extra id of kind `services`: every row, whatever its status.
SERVICES_ALL = "all"
ID_RE = re.compile(r"^[A-Za-z0-9@:._-]{1,120}\Z")
HEALTH_PREFIX = "health:"


def stats(values):
    """min / max / avg / p95 (nearest rank) / last of a non-empty list; all None for an empty one."""
    if not values:
        return {"min": None, "max": None, "avg": None, "p95": None, "last": None}
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return {"min": ordered[0], "max": ordered[-1], "avg": round(sum(values) / len(values), 4),
            "p95": ordered[rank - 1], "last": values[-1]}


def _stem(unit):
    return re.sub(r"\.(service|timer)$", "", unit or "")


class Detail:
    def __init__(self, cfg, state_cache, ttl=15.0, clock=time.time, games_cache=None, limits_cache=None,
                 usage_index=None):
        self.cfg, self._state_cache, self._clock = cfg, state_cache, clock
        self._games = games_cache
        self._limits, self._usage = limits_cache, usage_index
        self._cache = hud_state.KeyedCache(ttl)

    # ------------------------------------------------------------ entry point
    def get(self, kind, ident):
        """(http status, JSON-able dict)."""
        if kind not in KINDS:
            return 404, {"error": "unknown kind"}
        if not isinstance(ident, str) or not ID_RE.match(ident):
            return 400, {"error": "bad id"}
        # the claude ids are a fixed set: skipping the state build keeps a detail open from starting a limits fetch
        snap = None if kind == "claude" else self._state_cache.get()
        if ident not in self._allowed(kind, snap):
            return 404, {"error": "not found", "kind": kind}
        payload = self._cache.get((kind, ident), lambda: self._build(kind, ident, snap))
        return 200, payload

    @staticmethod
    def _allowed(kind, snap):
        if kind == "service":
            return {r["name"] for r in snap["services"]}
        if kind == "services":
            return set(STATUSES) | {SERVICES_ALL}
        if kind == "perf":
            return set(PERF_IDS)
        if kind == "site":
            return {s["host"] for s in snap["web"]["sites"]}
        if kind == "chat":
            return {s["host"] for s in snap["web"]["sites"] if "chat" in s}
        if kind == "game":
            return {r["name"] for r in snap.get("games", {}).get("servers", []) if r["name"] in games.GAME_SERVERS}
        if kind == "claude":
            return set(CLAUDE_IDS)   # always present: an unavailable source still opens a window that says so
        return {a["name"] for a in snap["alerts"]}

    def _build(self, kind, ident, snap):
        out = getattr(self, "_" + kind)(ident, snap)
        out.update(kind=kind, id=ident)
        return out

    # ------------------------------------------------------------ services
    def _services(self, status, snap):
        every = status == SERVICES_ALL
        rows = [{"name": r["name"], "port": r.get("port"), "label": r["label"], "reason": r["reason"],
                 "group": r["group"], "status": r["status"]} for r in snap["services"] if every or r["status"] == status]
        out = {"status": status, "label": "All" if every else classify.LABELS[status], "rows": rows,
               "total": len(snap["services"]), "counts": snap["meta"]["counts"]}
        if every:   # lets the window say "NO DATA, Prometheus unreachable" instead of presenting grey rows as fact
            out["summary"] = snap["meta"]["services_summary"]
        return out

    def _related_alerts(self, name, unit, snap):
        needles = {name.lower(), _stem(unit).lower()} - {""}
        return [{"name": a["name"], "severity": a["severity"], "summary": a["summary"]} for a in snap["alerts"]
                if any(n in a["name"].lower() or n in a["summary"].lower() for n in needles)]

    def _service(self, name, snap):
        row = next(r for r in snap["services"] if r["name"] == name)
        reg = next((s for s in sources.load_services(self.cfg.registry_path) if s["name"] == name), None) or {}
        now = self._clock()
        vals = sources.prom_service_values(self.cfg.prom_url)
        ok = vals["state"] == "ok"
        unit = reg.get("unit")
        junit = journal.journal_unit(unit)
        scopes = journal.scopes_for(reg) if reg else []

        def keyed(table, u):
            for sc in scopes:
                if (sc, u) in vals.get(table, {}):
                    return sc, vals[table][(sc, u)]
            return None, None

        scope_hit, active = keyed("unit_active", junit) if ok and junit else (None, None)
        scope_f, failed = keyed("unit_failed", junit) if ok and junit else (None, None)
        scope_r, r30 = keyed("restarts_30m", junit) if ok and junit else (None, None)
        _, r24 = keyed("restarts_24h", junit) if ok and junit else (None, None)
        timers = []
        if ok:
            for tu in (reg.get("unit"), reg.get("unit_db")):
                if tu and tu.endswith(".timer"):
                    sc, ts = keyed("timer_success", journal.journal_unit(tu))
                    _, res = keyed("timer_result", journal.journal_unit(tu))
                    timers.append({"unit": tu, "age_s": None if ts is None else max(0.0, now - ts),
                                   "last_ok": None if res is None else res == 1})
                    scope_hit = scope_hit or sc
        port_up = vals["port_up"].get(name) if ok and row.get("port") else None
        if not row.get("port"):
            probe = "no_port"
        elif port_up is None:
            probe = "no_data"
        else:
            probe = "open" if port_up == 1 else "closed"
        jr = (journal.tail(junit, scopes, binary=self.cfg.journalctl) if junit
              else {"state": "no_unit", "lines": [], "scope": None})
        jscope = jr.pop("scope", None)
        scope = scope_hit or scope_f or scope_r or jscope or (scopes[0] if len(scopes) == 1 else None)
        return {
            "state": "ok" if ok else "unreachable", "scope": scope, "port": row.get("port"), "probe": probe,
            "status": row["status"], "label": row["label"], "reason": row["reason"], "group": row["group"],
            "kind_of": reg.get("kind"), "unit": unit,
            "unit_active": None if active is None else active == 1,
            "unit_failed": None if failed is None else failed == 1,
            "restarts_30m": r30, "restarts_24h": r24,
            "timer_age_s": timers[0]["age_s"] if timers else None, "timers": timers,
            "related_alerts": self._related_alerts(name, unit, snap),
            "journal": jr,
        }

    # ------------------------------------------------------------ perf
    def _perf(self, metric, snap):
        res = sources.prom_perf_series(self.cfg.prom_url, metric, self._clock())
        series = [round(v, 3) for v in res["series"]]
        gauge = snap["perf"].get("uptime_s") if metric == "uptime" else snap["perf"].get(metric)
        out = {"state": res["state"], "metric": metric, "series": series, "step_s": sources.DETAIL_STEP,
               "thresholds": classify.thresholds_for(metric), "extras": res["extras"],
               "level": gauge.get("level") if isinstance(gauge, dict) else None,
               "unit": "load" if metric == "load" else "" if metric == "uptime" else "%"}
        out.update(stats(series))
        return out

    # ------------------------------------------------------------ website
    def _site(self, host, snap):
        site = next(s for s in snap["web"]["sites"] if s["host"] == host)
        sdef = next(s for s in hud_state.SITES if s["host"] == host)
        beacon = dict(site["beacon"])
        if beacon.get("state") == "ok":
            beacon.update(sources.beacon_detail(self.cfg.analytics_db, sdef["beacon"], self._clock()))
        return {"host": host, "zone": site["zone"], "beacon": beacon, "cf": site["cf"]}

    # ------------------------------------------------------------ chat
    def _chat(self, host, snap):
        out = chat.chat_detail(self.cfg.chat_dir, self.cfg.analytics_db, self._clock())
        out["host"] = host
        return out

    # ------------------------------------------------------------ game
    def _game(self, name, snap):
        row = next(r for r in snap["games"]["servers"] if r["name"] == name)
        return games.game_detail(name, row, self._games.get() if self._games else None, self._clock())

    # ------------------------------------------------------------ claude
    def _claude(self, ident, snap):
        if ident == "limits":
            if self._limits is None:
                return {"state": "unavailable"}
            out = hud_state.limits_view(self._limits.get(diag=True, fetch=False))   # a detail open never triggers a fetch
            out["thresholds"] = {"amber": classify.USAGE_AMBER, "red": classify.USAGE_RED}
            return out
        if self._usage is None:
            return {"state": "unavailable"}
        return self._usage.detail(ident, self._clock())

    # ------------------------------------------------------------ alert
    def _alert(self, name, snap):
        alert = next(a for a in snap["alerts"] if a["name"] == name)
        out = {"name": name, "severity": alert["severity"], "source": alert["source"], "summary": alert["summary"],
               "since": None, "labels": {}, "annotations": {}, "runs": [], "state": "ok"}
        if alert["source"] == "prometheus":
            st, raw = sources.prom_alert(self.cfg.prom_url, name)
            out["state"] = st
            if raw:
                out.update(labels=raw["labels"], annotations=raw["annotations"], since=raw["since"])
        elif alert["source"] == "health":
            check = name[len(HEALTH_PREFIX):] if name.startswith(HEALTH_PREFIX) else None
            st, runs, since = sources.health_runs(self.cfg.product_db, check)
            out.update(state=st, runs=runs, since=since)
        return out
