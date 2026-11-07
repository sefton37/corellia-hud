"""Stoplight classification. The ONLY place colour is decided; the page renders what it is given.

green = up, amber = degraded / stale / restart-looping, red = down / failed, grey = no data.
A grey lamp is never produced by "assume fine": no signal means grey, never green.
"""
import math
from dataclasses import dataclass, field

GREEN, AMBER, RED, GREY = "green", "amber", "red", "grey"
LABELS = {GREEN: "UP", AMBER: "WARN", RED: "DOWN", GREY: "NO DATA"}
_RANK = {GREEN: 0, AMBER: 1, RED: 2}

STALE_SECONDS = 600            # mirrors the CollectorStale rule
RESTART_LOOP_THRESHOLD = 5     # mirrors UnitRestartLoop: increase(restarts[30m]) > 5
HOUR = 3600
# Mirrors BackupStaleNightly (30 h) and BackupStaleHourly (3 h); other timers get the default.
TIMER_MAX_AGE = {"corellia-backup.service": 30 * HOUR, "corellia-backup-db.service": 3 * HOUR}
DEFAULT_TIMER_MAX_AGE = 48 * HOUR
# Scope comes from the registry `kind`: flint exists in BOTH scopes (system=0, user=1).
SCOPE_FOR_KIND = {"systemd": "system", "systemd-user": "user"}


@dataclass
class PromView:
    """Everything the classifier needs from Prometheus, already parsed into plain dicts."""
    ok: bool = False
    now: float = 0.0
    collector_age: float = None
    port_up: dict = field(default_factory=dict)          # {service: value}
    unit_active: dict = field(default_factory=dict)      # {(scope, unit): value}
    unit_failed: dict = field(default_factory=dict)
    restart_increase: dict = field(default_factory=dict)  # increase over 30 m
    timer_success: dict = field(default_factory=dict)    # {(scope, unit): epoch seconds}
    timer_result: dict = field(default_factory=dict)     # {(scope, unit): 1 ok / 0 failed}


def num(value):
    """A finite float, or None for NaN / inf / non-numeric samples."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _timer_signals(view, scope, unit):
    name = unit[:-len(".timer")] + ".service" if unit.endswith(".timer") else unit
    out = []
    ok = num(view.timer_success.get((scope, name)))
    result = num(view.timer_result.get((scope, name)))
    if result is not None and result == 0:
        out.append((RED, f"{name} last run failed"))
    if ok is not None:
        age = view.now - ok
        if age > TIMER_MAX_AGE.get(name, DEFAULT_TIMER_MAX_AGE):
            out.append((AMBER, f"{name} last success {int(age // HOUR)} h ago"))
        else:
            out.append((GREEN, f"{name} ran {int(age // HOUR)} h ago"))
    return out


def _unit_signals(view, scope, unit):
    out = []
    key = (scope, unit)
    failed = num(view.unit_failed.get(key))
    active = num(view.unit_active.get(key))
    loops = num(view.restart_increase.get(key))
    if failed is not None and failed == 1:
        out.append((RED, "unit failed"))
    if active is not None:
        out.append((GREEN, "unit active") if active == 1 else (RED, "unit inactive"))
    if loops is not None and loops > RESTART_LOOP_THRESHOLD:
        out.append((AMBER, f"restart loop ({int(loops)} in 30 min)"))
    return out


def _signals(svc, view):
    sig = []
    port = num(view.port_up.get(svc["name"])) if svc.get("port") else None
    if port is not None:
        sig.append((GREEN, f"port {svc['port']} open") if port == 1 else (RED, "port closed"))
    kind = svc.get("kind")
    if kind in SCOPE_FOR_KIND and svc.get("unit"):
        scope = SCOPE_FOR_KIND[kind]
        for unit in (svc["unit"], svc.get("unit_db")):
            if not unit:
                continue
            if unit.endswith(".timer"):
                sig.extend(_timer_signals(view, scope, unit))
            else:
                sig.extend(_unit_signals(view, scope, unit))
    return sig


def classify_service(svc, view):
    """Return {name, status, label, reason, stale} for one registry service."""
    stale = (not view.ok) or (num(view.collector_age) is None) or view.collector_age > STALE_SECONDS
    if not view.ok:
        return _result(svc, GREY, "prometheus unreachable", False)
    sig = _signals(svc, view)
    if not sig:
        status = GREY
        reason = "no probe" if not (svc.get("port") or svc.get("unit")) else "no series"
        return _result(svc, status, reason, stale)
    status, reason = max(sig, key=lambda s: _RANK[s[0]])
    if stale and status == GREEN:
        status, reason = AMBER, "stale: collector silent"
    return _result(svc, status, reason, stale)


def _result(svc, status, reason, stale):
    return {"name": svc["name"], "status": status, "label": LABELS[status], "reason": reason, "stale": bool(stale)}


# Game servers: the unit lamp is the floor; a game-specific probe may only RAISE green to amber.
_GAME_PROBE_AMBER = {
    "offline": "Flint reports the server offline while its unit is active",
    "no_response": "server-list query not answering while the unit is active",
    "unparseable": "server-list reply unreadable while the unit is active",
}


def game_lamp(unit_row, probe):
    """(status, reason) for one game server. `unit_row` is the classified registry row of its unit (or None);
    `probe` is {"state": ...} from the game's own source. Failed / inactive / looping / no-data units keep
    their lamp whatever the probe says; only an active unit can be raised to amber, never to red."""
    if not unit_row:
        return GREY, "not in the registry"
    status, reason = unit_row["status"], unit_row["reason"]
    if status != GREEN:
        return status, reason
    state = (probe or {}).get("state")
    if state in _GAME_PROBE_AMBER:
        return AMBER, _GAME_PROBE_AMBER[state]
    return GREEN, reason


# Perf gauges: (amber at, red at), as percentages of capacity.
_LEVELS = {"cpu": (85, 97), "mem": (85, 95), "disk": (85, 95), "gpu": (95, 101), "vram": (92, 99)}


def level_for(metric, value):
    v = num(value)
    if v is None:
        return GREY
    amber, red = _LEVELS.get(metric, (101, 101))
    return RED if v >= red else AMBER if v >= amber else GREEN


def thresholds_for(metric):
    """{amber, red} percentages for a perf gauge, or None when the metric has no thresholds (load, uptime)."""
    if metric not in _LEVELS:
        return None
    amber, red = _LEVELS[metric]
    return {"amber": amber, "red": red}


# Claude usage bars: percent of a limit USED. Invented policy (like the perf gauges); never an alert.
USAGE_AMBER, USAGE_RED = 80, 95


def usage_level(pct):
    """grey for no / non-finite value, green below 80, amber from 80, red from 95."""
    v = num(pct)
    if v is None:
        return GREY
    return RED if v >= USAGE_RED else AMBER if v >= USAGE_AMBER else GREEN


# Services panel summary (Spec #492). The page renders `level` and never derives a colour of its own.
SERVICES_LIST_CAP = 4
_NOT_UP_ORDER = {RED: 0, AMBER: 1, GREY: 2}   # an unknown status sorts after grey


def services_summary(rows, sources, cap=SERVICES_LIST_CAP):
    """The compact Services panel's decision, pure. `rows` are classified service rows, `sources` is meta.sources.

    state precedence: no_registry > unavailable (Prometheus not answering) > stale (collector silent) > empty >
    all_up (every row green, every source ok) > problems. `all_up` is the only state that may read as "up".
    `listed` holds service NAMES, worst first (red, amber, grey; stable within a status), at most `cap`, and only
    for `problems`; a row whose status is not one of the four known values counts as not up."""
    rows, src, cap = list(rows), sources or {}, max(int(cap), 0)
    up = sum(1 for r in rows if r.get("status") == GREEN)
    total = len(rows)
    not_up = total - up
    prom = src.get("prometheus")
    if src.get("registry") != "ok":
        state, level = "no_registry", GREY
    elif prom == "stale":
        state, level = "stale", AMBER
    elif prom != "ok":
        state, level = "unavailable", GREY
    elif total == 0:
        state, level = "empty", GREY
    elif not_up == 0:
        state, level = "all_up", GREEN
    else:
        state = "problems"
        bad = sorted((r for r in rows if r.get("status") != GREEN),
                     key=lambda r: _NOT_UP_ORDER.get(r.get("status"), len(_NOT_UP_ORDER)))
        worst = bad[0].get("status")
        level = worst if worst in _NOT_UP_ORDER else GREY
    listed = []
    if state == "problems":
        listed = [r["name"] for r in bad[:cap]]
    return {"state": state, "level": level, "up": up, "total": total, "not_up": not_up,
            "listed": listed, "more": not_up - len(listed) if state == "problems" else 0, "cap": cap}
