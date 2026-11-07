"""Data sources: registry, Prometheus, beacon DB, health-check DB, Cloudflare.

Every function fails soft by RETURNING an explicit state; none of them invents a number.
Everything is read-only: Prometheus is GET-only, SQLite is opened `mode=ro`, and the single
POST is the Cloudflare GraphQL *query*. The token only ever travels in the request header and is
never put into a message, a return value, or an exception text.
"""
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import classify

# ---------------------------------------------------------------- registry

EXCLUDED_STATUS = {"retired", "deferred"}
EXCLUDED_KINDS = {"android-adb", "steam-proton-game"}  # not hosted on this machine
SELF_NAME = "corellia-hud"                              # the HUD never lists itself
_NAME = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(#.*)?$")
_FIELD = re.compile(r"^    ([a-z_-]+):\s*(.*?)\s*$")


def parse_registry(path):
    """Minimal line parser for the `services:` block of registry.yaml (stdlib only, no PyYAML)."""
    rows, cur, in_services = [], None, False
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if re.match(r"^[A-Za-z_]", line):  # a new top-level key
                in_services = line.startswith("services:")
                cur = None
                continue
            if not in_services or not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _NAME.match(line)
            if m:
                cur = {"name": m.group(1)}
                rows.append(cur)
                continue
            m = _FIELD.match(line)
            if m and cur is not None:
                value = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip("'\"")
                cur[m.group(1)] = value
    return rows


def _to_service(raw):
    port = raw.get("port", "")
    return {
        "name": raw["name"],
        "kind": raw.get("kind", ""),
        "status": raw.get("status", ""),
        "port": int(port) if str(port).isdigit() else None,
        "unit": raw.get("unit") or None,
        "unit_db": raw.get("unit_db") or None,
        "unit_path": raw.get("unit-path") or None,
    }


def load_services(path):
    """Hosted, active services from the registry (raises OSError if the file is missing)."""
    out = []
    for raw in parse_registry(path):
        s = _to_service(raw)
        if s["status"] in EXCLUDED_STATUS or s["kind"] in EXCLUDED_KINDS or s["name"] == SELF_NAME:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------- prometheus

VRAM_TOTAL_BYTES = int(os.environ.get("HUD_VRAM_TOTAL_BYTES", 16376 * 1024 * 1024))  # RTX 4070 Ti SUPER

INSTANT = {
    "port_up": "corellia_service_port_up",
    "unit_active": "corellia_unit_active",
    "unit_failed": "corellia_unit_failed",
    "restarts": "increase(corellia_unit_restarts_total[30m])",
    "timer_success": "corellia_timer_last_success_seconds",
    "timer_result": "corellia_timer_last_result",
    "collector_age": "time() - corellia_collector_last_run_seconds",
    "cpu": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m])))',
    "mem": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "disk": '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
    "gpu_util": "corellia_gpu_utilization_percent",
    "gpu_mem": "corellia_gpu_memory_used_bytes",
    "load": "node_load1",
    "uptime": "time() - node_boot_time_seconds",
}
RANGE = {"cpu": INSTANT["cpu"], "mem": INSTANT["mem"], "gpu_util": INSTANT["gpu_util"], "load": INSTANT["load"]}
SPARK_POINTS = 60


def _http_json(url, timeout, method="GET", headers=None, body=None):
    """(status, parsed-json-or-None). HTTP error statuses are returned, transport errors raise."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def _prom_get(base, path, params, timeout):
    """One Prometheus GET; None on any failure (the caller decides what that means)."""
    try:
        status, body = _http_json(f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(params)}", timeout)
    except Exception:
        return None
    if status != 200 or not isinstance(body, dict) or body.get("status") != "success":
        return None
    return body.get("data")


def _vector(data):
    return (data or {}).get("result", []) if isinstance(data, dict) else []


def _scalar(data):
    for item in _vector(data):
        try:
            return classify.num(item["value"][1])
        except (KeyError, IndexError, TypeError):
            continue
    return None


def _keyed(data, *labels):
    out = {}
    for item in _vector(data):
        try:
            key = tuple(item["metric"][l] for l in labels)
            out[key[0] if len(labels) == 1 else key] = classify.num(item["value"][1])
        except (KeyError, IndexError, TypeError):
            continue
    return out


def _spark(data):
    for item in _vector(data):
        pts = [classify.num(v[1]) for v in item.get("values", []) if len(v) > 1]
        pts = [round(p, 2) for p in pts if p is not None]
        return pts[-SPARK_POINTS:]
    return []


def _gauge(pct, spark=None, level=None, **extra):
    d = {"pct": None if pct is None else round(pct, 1), "spark": spark or [],
         "level": classify.GREY if pct is None else level}
    d.update(extra)
    return d


def empty_perf():
    return {k: _gauge(None) for k in ("cpu", "mem", "disk", "gpu", "vram")} | {
        "load": {"value": None, "spark": [], "level": classify.GREY}, "uptime_s": None}


def fetch_prometheus(url, now, timeout=2.0):
    """Query Prometheus in parallel. Returns {state, view, perf, alerts}; never raises."""
    jobs = {("i", k): ("/api/v1/query", {"query": q}) for k, q in INSTANT.items()}
    start = int(now) - 3600
    for k, q in RANGE.items():
        jobs[("r", k)] = ("/api/v1/query_range", {"query": q, "start": start, "end": int(now), "step": 60})
    jobs[("a", "alerts")] = ("/api/v1/alerts", {})
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {k: pool.submit(_prom_get, url, p, params, timeout) for k, (p, params) in jobs.items()}
        got = {k: f.result() for k, f in futures.items()}
    inst = {k: got[("i", k)] for k in INSTANT}
    if inst["port_up"] is None:
        return {"state": "unreachable", "view": classify.PromView(ok=False, now=now),
                "perf": empty_perf(), "alerts": []}
    age = _scalar(inst["collector_age"])
    view = classify.PromView(
        ok=True, now=now, collector_age=age,
        port_up=_keyed(inst["port_up"], "service"),
        unit_active=_keyed(inst["unit_active"], "scope", "unit"),
        unit_failed=_keyed(inst["unit_failed"], "scope", "unit"),
        restart_increase=_keyed(inst["restarts"], "scope", "unit"),
        timer_success=_keyed(inst["timer_success"], "scope", "unit"),
        timer_result=_keyed(inst["timer_result"], "scope", "unit"))
    state = "ok" if age is not None and age <= classify.STALE_SECONDS else "stale"
    return {"state": state, "view": view, "perf": _perf(inst, got), "alerts": _alerts(got[("a", "alerts")])}


def _perf(inst, got):
    sp = {k: _spark(got[("r", k)]) for k in RANGE}
    cpu, mem, disk = (_scalar(inst[k]) for k in ("cpu", "mem", "disk"))
    gpu, vram_used = _scalar(inst["gpu_util"]), _scalar(inst["gpu_mem"])
    vram = None if vram_used is None else vram_used / VRAM_TOTAL_BYTES * 100
    load = _scalar(inst["load"])
    return {
        "cpu": _gauge(cpu, sp["cpu"], classify.level_for("cpu", cpu)),
        "mem": _gauge(mem, sp["mem"], classify.level_for("mem", mem)),
        "disk": _gauge(disk, None, classify.level_for("disk", disk)),
        "gpu": _gauge(gpu, sp["gpu_util"], classify.level_for("gpu", gpu)),
        "vram": _gauge(vram, None, classify.level_for("vram", vram), used_bytes=vram_used,
                       total_bytes=VRAM_TOTAL_BYTES),
        "load": {"value": None if load is None else round(load, 2), "spark": sp["load"],
                 "level": classify.GREY if load is None else classify.GREEN},
        "uptime_s": _scalar(inst["uptime"]),
    }


def _alerts(data):
    out = []
    for a in (data or {}).get("alerts", []) if isinstance(data, dict) else []:
        if a.get("state") != "firing":
            continue
        labels, ann = a.get("labels", {}), a.get("annotations", {})
        name = labels.get("alertname", "alert")
        out.append({"name": name, "severity": labels.get("severity", "warning"), "source": "prometheus",
                    "summary": ann.get("summary") or ann.get("description") or name})
    return out


# ---------------------------------------------------------------- prometheus: detail windows

DETAIL_STEP = 300          # 24 h at 300 s = 288 points
DETAIL_POINTS = 288
RESTARTS_24H = "increase(corellia_unit_restarts_total[24h])"
PERF_QUERIES = {"cpu": INSTANT["cpu"], "mem": INSTANT["mem"], "disk": INSTANT["disk"],
                "gpu": INSTANT["gpu_util"], "vram": INSTANT["gpu_mem"], "load": INSTANT["load"]}
DISK_SIZE = 'max(node_filesystem_size_bytes{mountpoint="/"})'
REBOOTS_30D = "changes(node_boot_time_seconds[30d])"


def prom_service_values(url, timeout=2.0):
    """Per-unit raw values for one service window. Fixed queries (no registry name is ever interpolated
    into PromQL); the caller picks its own unit out of the result. {"state": "unreachable"} on failure."""
    jobs = {"port_up": INSTANT["port_up"], "unit_active": INSTANT["unit_active"],
            "unit_failed": INSTANT["unit_failed"], "restarts_30m": INSTANT["restarts"],
            "restarts_24h": RESTARTS_24H, "timer_success": INSTANT["timer_success"],
            "timer_result": INSTANT["timer_result"]}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {k: pool.submit(_prom_get, url, "/api/v1/query", {"query": q}, timeout) for k, q in jobs.items()}
        got = {k: f.result() for k, f in futs.items()}
    if got["port_up"] is None:
        return {"state": "unreachable"}
    return {"state": "ok", "port_up": _keyed(got["port_up"], "service"),
            **{k: _keyed(got[k], "scope", "unit") for k in jobs if k != "port_up"}}


def prom_restarts_24h(url, timeout=2.0):
    """{(scope, unit): restarts in the last 24 h} from the fixed query, or None when Prometheus does not answer."""
    data = _prom_get(url, "/api/v1/query", {"query": RESTARTS_24H}, timeout)
    return None if data is None else _keyed(data, "scope", "unit")


def _series_values(data):
    for item in _vector(data):
        pts = [classify.num(v[1]) for v in item.get("values", []) if len(v) > 1]
        return [p for p in pts if p is not None][-DETAIL_POINTS:]
    return []


def prom_perf_series(url, metric, now, timeout=3.0):
    """{state, series, extras} for one perf tile over the last 24 h. state: ok | unreachable | no_data."""
    if metric == "uptime":
        with ThreadPoolExecutor(max_workers=2) as pool:
            up = pool.submit(_prom_get, url, "/api/v1/query", {"query": INSTANT["uptime"]}, timeout)
            reb = pool.submit(_prom_get, url, "/api/v1/query", {"query": REBOOTS_30D}, timeout)
            up_s, reboots = _scalar(up.result()), _scalar(reb.result())
        if up_s is None:
            return {"state": "unreachable", "series": [], "extras": {}}
        return {"state": "ok", "series": [], "extras": {
            "uptime_s": up_s, "boot_time": now - up_s, "reboots_30d": None if reboots is None else int(reboots)}}
    params = {"query": PERF_QUERIES[metric], "start": int(now) - 86400, "end": int(now), "step": DETAIL_STEP}
    data = _prom_get(url, "/api/v1/query_range", params, timeout)
    if data is None:
        return {"state": "unreachable", "series": [], "extras": {}}
    series = _series_values(data)
    extras = {}
    if series and metric == "vram":
        extras = {"used_bytes": series[-1], "total_bytes": VRAM_TOTAL_BYTES}
        series = [v / VRAM_TOTAL_BYTES * 100 for v in series]
    elif series and metric == "disk":
        total = _scalar(_prom_get(url, "/api/v1/query", {"query": DISK_SIZE}, timeout))
        extras = {"total_bytes": total, "used_bytes": None if total is None else total * series[-1] / 100}
    return {"state": "ok" if series else "no_data", "series": series, "extras": extras}


def _active_epoch(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def prom_alert(url, name, timeout=2.0):
    """(state, alert) for one firing Prometheus alert by name; alert is None when it is not firing."""
    data = _prom_get(url, "/api/v1/alerts", {}, timeout)
    if data is None:
        return "unreachable", None
    for a in data.get("alerts", []) if isinstance(data, dict) else []:
        labels = a.get("labels", {}) or {}
        if a.get("state") == "firing" and labels.get("alertname") == name:
            return "ok", {"labels": {str(k): str(v) for k, v in labels.items()},
                          "annotations": {str(k): str(v) for k, v in (a.get("annotations") or {}).items()},
                          "since": _active_epoch(a.get("activeAt"))}
    return "ok", None


# ---------------------------------------------------------------- sqlite (read-only)


def _utc(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _epoch(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _open_ro(path):
    return sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True, timeout=2)


open_ro = _open_ro  # the one read-only door; chat.py reuses it so every DB open goes through here


MIN_DELTA_SAMPLE = 10  # below this in both periods a percentage is noise (0 vs 1 reads "-100%")


def delta(cur, prev):
    """Percent change, "new" when the prior period was empty, None when there is nothing to compare,
    and the prior raw count ("was N") when both periods are too small for a percentage to mean anything."""
    if prev == 0:
        return {"label": "new"} if cur > 0 else None
    if max(cur, prev) < MIN_DELTA_SAMPLE:
        return {"label": f"was {prev}"}
    return {"pct": round((cur - prev) / prev * 100)}


def beacon_stats(db_path, key, now):
    """Visitors (distinct visitor_hash on pageview events) for the last 24 h / 7 d with deltas."""
    if not os.path.exists(db_path):
        return {"state": "missing"}
    day, week = 86400, 7 * 86400
    sql = ("SELECT COUNT(DISTINCT visitor_hash), COUNT(*) FROM page_events "
           "WHERE site = ? AND event_type = 'pageview' AND created_at >= ? AND created_at < ?")
    try:
        con = _open_ro(db_path)
        try:
            def window(a, b):
                return con.execute(sql, (key, _utc(a), _utc(b))).fetchone()
            far = now + day
            v24, p24 = window(now - day, far)
            v7, _ = window(now - week, far)
            prev24 = window(now - 2 * day, now - day)[0]
            prev7 = window(now - 2 * week, now - week)[0]
        finally:
            con.close()
    except sqlite3.Error:
        return {"state": "unreachable"}
    return {"state": "ok", "visitors_24h": v24, "visitors_7d": v7, "pageviews_24h": p24,
            "delta_24h": delta(v24, prev24), "delta_7d": delta(v7, prev7)}


HEALTH_STALE_SECONDS = 9 * 86400  # the health-check timer is weekly
BATCH_WINDOW_SECONDS = 600


def health_alerts(db_path, now):
    """(state, alerts) from product.db health_check_runs: latest-batch failures + staleness."""
    if not os.path.exists(db_path):
        return "missing", []
    try:
        con = _open_ro(db_path)
        try:
            last = con.execute("SELECT MAX(run_at) FROM health_check_runs").fetchone()[0]
            if last is None:
                return "ok", [_health_stale("no health-check run recorded")]
            batch_start = _utc(_epoch(last) - BATCH_WINDOW_SECONDS)
            fails = [r[0] for r in con.execute(
                "SELECT DISTINCT check_id FROM health_check_runs WHERE result = 'fail' AND run_at >= ? "
                "ORDER BY check_id", (batch_start,))]
        finally:
            con.close()
    except (sqlite3.Error, ValueError):
        return "unreachable", []
    alerts = []
    if now - _epoch(last) > HEALTH_STALE_SECONDS:
        alerts.append(_health_stale(f"last health-check run was {int((now - _epoch(last)) // 86400)} days ago"))
    alerts += [{"name": f"health:{cid}", "severity": "warning", "source": "health",
                "summary": f"health check {cid} failed in the latest run"} for cid in fails]
    return "ok", alerts


def _health_stale(summary):
    return {"name": "HealthChecksStale", "severity": "warning", "source": "health", "summary": summary}


def health_runs(db_path, check_id, limit=5):
    """(state, runs newest-first, since_epoch) for one health check; check_id None means any check."""
    if not os.path.exists(db_path):
        return "missing", [], None
    try:
        con = _open_ro(db_path)
        try:
            if check_id is None:
                rows = con.execute("SELECT check_id, run_at, result FROM health_check_runs "
                                   "ORDER BY run_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = con.execute("SELECT check_id, run_at, result FROM health_check_runs WHERE check_id = ? "
                                   "ORDER BY run_at DESC, id DESC LIMIT ?", (check_id, limit)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return "unreachable", [], None
    runs = [{"check_id": c, "run_at": r, "result": res} for c, r, res in rows]
    try:
        since = _epoch(rows[0][1]) if rows else None
    except ValueError:
        since = None
    return "ok", runs, since


def beacon_detail(db_path, key, now):
    """Top pages / referrers, 7 UTC-day series and countries for one beacon site (all read-only)."""
    if not os.path.exists(db_path):
        return {"state": "missing"}
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    since = days[0] + " 00:00:00"
    base = "FROM page_events WHERE site = ? AND event_type = 'pageview' AND created_at >= ?"
    try:
        con = _open_ro(db_path)
        try:
            pages = con.execute(f"SELECT path, COUNT(*), COUNT(DISTINCT visitor_hash) {base} GROUP BY path "
                                "ORDER BY 2 DESC, path LIMIT 10", (key, since)).fetchall()
            refs = con.execute(f"SELECT referrer, COUNT(*) {base} AND referrer IS NOT NULL AND referrer != '' "
                               "GROUP BY referrer ORDER BY 2 DESC, referrer LIMIT 10", (key, since)).fetchall()
            per_day = {d: (v, p) for d, v, p in con.execute(
                f"SELECT substr(created_at, 1, 10), COUNT(DISTINCT visitor_hash), COUNT(*) {base} GROUP BY 1",
                (key, since))}
            try:
                countries = con.execute("SELECT country, SUM(pageviews) FROM daily_countries WHERE site = ? AND date >= ? "
                                        "GROUP BY country ORDER BY 2 DESC, country LIMIT 10", (key, days[0])).fetchall()
                countries_state = "ok"
            except sqlite3.OperationalError:
                countries, countries_state = [], "missing"
        finally:
            con.close()
    except sqlite3.Error:
        return {"state": "unreachable"}
    return {
        "state": "ok",
        "top_pages": [{"path": p, "views": n, "visitors": v} for p, n, v in pages],
        "top_referrers": [{"referrer": r, "views": n} for r, n in refs],
        "daily_7d": [{"date": d, "visitors": per_day.get(d, (0, 0))[0], "pageviews": per_day.get(d, (0, 0))[1]}
                     for d in days],
        "countries": [{"country": c, "pageviews": int(n or 0)} for c, n in countries],
        "countries_state": countries_state,
    }


def disk_alert(used_pct):
    """Mirrors DiskLow: root filesystem under 10% free."""
    if used_pct is None:
        return None
    free = 100 - used_pct
    if free < 10:
        return {"name": "DiskLow", "severity": "warning", "source": "local",
                "summary": f"root filesystem {free:.0f}% free"}
    return None


def merge_alerts(*lists):
    """Union by name; the first list to mention a name wins (so Prometheus beats local copies)."""
    seen, out = set(), []
    for lst in lists:
        for a in lst:
            if a and a["name"] not in seen:
                seen.add(a["name"])
                out.append(a)
    return out


# ---------------------------------------------------------------- cloudflare

CF_QUERY = """query HudSite($zone: String!, $host: String!, $a24: Time!, $a48: Time!, $a7: Time!, $a14: Time!, $now: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    d1: httpRequestsAdaptiveGroups(limit: 1, filter: {clientRequestHTTPHost: $host, datetime_geq: $a24, datetime_lt: $now}) { count }
    p1: httpRequestsAdaptiveGroups(limit: 1, filter: {clientRequestHTTPHost: $host, datetime_geq: $a48, datetime_lt: $a24}) { count }
    d7: httpRequestsAdaptiveGroups(limit: 1, filter: {clientRequestHTTPHost: $host, datetime_geq: $a7, datetime_lt: $now}) { count }
    p7: httpRequestsAdaptiveGroups(limit: 1, filter: {clientRequestHTTPHost: $host, datetime_geq: $a14, datetime_lt: $a7}) { count }
    t1: firewallEventsAdaptiveGroups(limit: 1, filter: {clientRequestHTTPHost: $host, datetime_geq: $a24, datetime_lt: $now}) { count }
    t7: firewallEventsAdaptiveGroups(limit: 1, filter: {clientRequestHTTPHost: $host, datetime_geq: $a7, datetime_lt: $now}) { count }
  } }
}"""


def read_token(path):
    """(state, token): token_needed if absent/empty, token_insecure if group/other can read it."""
    try:
        st = os.stat(path)
        if st.st_mode & 0o077:
            return "token_insecure", None
        with open(path) as f:
            token = f.read().strip()
    except OSError:
        return "token_needed", None
    return ("ok", token) if token else ("token_needed", None)


def _rfc3339(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cf_error(detail):
    return {"state": "cf_error", "detail": detail}


def _cf_site(api_base, headers, zone_id, host, now, timeout):
    day = 86400
    variables = {"zone": zone_id, "host": host, "now": _rfc3339(now), "a24": _rfc3339(now - day),
                 "a48": _rfc3339(now - 2 * day), "a7": _rfc3339(now - 7 * day), "a14": _rfc3339(now - 14 * day)}
    body = json.dumps({"query": CF_QUERY, "variables": variables}).encode()
    status, resp = _http_json(f"{api_base}/graphql", timeout, "POST",
                              {**headers, "Content-Type": "application/json"}, body)
    if status != 200 or not isinstance(resp, dict):
        return _cf_error(f"graphql http {status}")
    if resp.get("errors") or not resp.get("data"):
        return _cf_error("graphql returned errors")
    try:
        zone = resp["data"]["viewer"]["zones"][0]
        n = {k: (zone[k][0]["count"] if zone[k] else 0) for k in ("d1", "p1", "d7", "p7", "t1", "t7")}
    except (KeyError, IndexError, TypeError):
        return _cf_error("unexpected graphql shape")
    return {"state": "ok", "requests_24h": n["d1"], "requests_7d": n["d7"],
            "threats_24h": n["t1"], "threats_7d": n["t7"],
            "delta_24h": delta(n["d1"], n["p1"]), "delta_7d": delta(n["d7"], n["p7"])}


def _cf_zone(api_base, headers, zone, hosts, now, timeout):
    status, resp = _http_json(f"{api_base}/zones?{urllib.parse.urlencode({'name': zone})}", timeout, "GET", headers)
    result = resp.get("result") if isinstance(resp, dict) else None
    if status != 200 or not result:
        return {h: _cf_error(f"zone lookup http {status}") for h in hosts}
    zone_id = result[0].get("id")
    return {h: _cf_site(api_base, headers, zone_id, h, now, timeout) for h in hosts}


def cloudflare_stats(sites, token_file, api_base, now, timeout=2.0, raise_errors=False):
    """Per-zone, per-site Cloudflare numbers. Each zone degrades independently; errors are never zeros."""
    zones = {}
    for s in sites:
        zones.setdefault(s["zone"], []).append(s["host"])
    tstate, token = read_token(token_file)
    if tstate != "ok":
        return {"state": tstate, "zones": [{"zone": z, "state": tstate} for z in zones],
                "sites": {s["host"]: {"state": tstate} for s in sites}}
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    base = api_base.rstrip("/")

    def one(zone):
        try:
            return _cf_zone(base, headers, zone, zones[zone], now, timeout)
        except Exception as exc:
            if raise_errors:
                raise
            return {h: _cf_error(type(exc).__name__) for h in zones[zone]}

    with ThreadPoolExecutor(max_workers=max(1, len(zones))) as pool:
        per_zone = dict(zip(zones, pool.map(one, zones)))
    out_sites, out_zones = {}, []
    for zone, hosts in zones.items():
        out_sites.update(per_zone[zone])
        states = {per_zone[zone][h]["state"] for h in hosts}
        out_zones.append({"zone": zone, "state": "ok" if states == {"ok"} else "cf_error"})
    overall = "ok" if any(z["state"] == "ok" for z in out_zones) else "cf_error"
    return {"state": overall, "zones": out_zones, "sites": out_sites}
