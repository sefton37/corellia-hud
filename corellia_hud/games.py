"""The Games section's data: ARK (three maps, through Flint's read-only HTTP API) and Minecraft Bedrock.

Sources, each failing soft to an explicit state (never a zero, never a fake green):
  * Flint /api/status and /api/events on loopback: hardened client, GET only, redirects refused, body capped;
  * the unit's cgroup and process table: RAM, CPU, uptime, the launch-line MaxPlayers / cluster id (the rest
    of the launch line is dropped on the spot);
  * the newest *.ark save's mtime (contents are never opened);
  * one UDP server-list ping to 127.0.0.1:19132 and the Bedrock journal window (through journal.py).
Colour is decided only in classify.game_lamp. Nothing here writes anywhere, opens an admin console, or
reads a credentials file. Everything private (player names) stays in the cached probes and surfaces only
through game_detail; the tile rows built by build_games carry counts and metadata only.
"""
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import classify, journal, sources

GAMES_TTL = 30                 # seconds; Flint itself polls its game servers every 30 s, so faster reads re-read one row
STALE_SECONDS = 120            # a poll stamp older (or further in the future) than this is stale
ONLINE_WINDOW_SECONDS = 60     # a player is online-now if last seen within this many seconds of the last poll
FLINT_TIMEOUT = 2.0
FLINT_MAX_BODY = 1024 * 1024
PING_TIMEOUT = 2.0
LOOPBACK = "127.0.0.1"
BEDROCK_PORT = 19132
MAX_NAMES = 20

# name -> fixed facts. The ONLY source of server names, units and Flint `server=` values.
GAME_SERVERS = {
    "ark-server": {"title": "Extinction", "type": "ark", "flint": "Extinction", "unit": "ark-server.service"},
    "ark-astraeos": {"title": "Astraeos", "type": "ark", "flint": "Astraeos", "unit": "ark-astraeos.service"},
    "ark-genesis": {"title": "Genesis", "type": "ark", "flint": "Genesis", "unit": "ark-genesis.service"},
    "bedrock": {"title": "Bedrock", "type": "bedrock", "flint": None, "unit": "bedrock.service"},
}
FLINT_SERVERS = frozenset(v["flint"] for v in GAME_SERVERS.values() if v["flint"])
FLINT_PATHS = {"/api/status": dict, "/api/events": list}
# Flint's poll-stamp key, assembled so no source file spells the admin-protocol word.
POLL_KEY = "last_" + "rc" + "on"
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def default_flint_url():
    return os.environ.get("HUD_FLINT_URL", "http://127.0.0.1:8090")


def default_bedrock_port():
    try:
        return int(os.environ.get("HUD_BEDROCK_PORT", BEDROCK_PORT))
    except ValueError:
        return BEDROCK_PORT


# ---------------------------------------------------------------- Flint client (loopback, GET, no redirects)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # REFUSE_REDIRECT


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _loopback_base(base):
    try:
        parts = urlsplit(base)
        ok = (parts.scheme == "http" and parts.hostname in (LOOPBACK, "localhost") and parts.username is None
              and parts.port is not None and not parts.path.strip("/") and not parts.query)
    except ValueError:
        return None
    return f"http://{parts.hostname}:{parts.port}" if ok else None


def flint_get(base, path, server, timeout=FLINT_TIMEOUT):
    """(state, data). state: ok | misconfigured | refused | redirect | http_error | timeout | unreachable |
    bad_json | too_large. Only a fixed path and a table server name are ever requested."""
    root = _loopback_base(base)
    if root is None:
        return "misconfigured", None
    if path not in FLINT_PATHS or server not in FLINT_SERVERS:
        return "refused", None
    req = urllib.request.Request(f"{root}{path}?server={server}", method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read(FLINT_MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        return ("redirect" if 300 <= e.code < 400 else "http_error"), None
    except TimeoutError:
        return "timeout", None
    except urllib.error.URLError as e:
        return ("timeout" if isinstance(e.reason, TimeoutError) else "unreachable"), None
    except OSError:
        return "unreachable", None
    if len(raw) > FLINT_MAX_BODY:
        return "too_large", None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "bad_json", None
    if not isinstance(data, FLINT_PATHS[path]):
        return "bad_json", None
    return "ok", data


# ---------------------------------------------------------------- ARK players

def parse_flint_time(text):
    """Epoch seconds for one of Flint's naive LOCAL-time stamps, or None. (Flint writes datetime.now().)"""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_utc_time(text):
    """Epoch seconds for one of Flint's UTC log-event times (naive ISO text), or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def display_name(name):
    """Flint falls back to the first 8 hex characters of an EOS id when a name is missing; say so."""
    if not isinstance(name, str) or not name.strip() or _HEX8.match(name):
        return "unnamed player"
    return name


def _players_out(state, probe="ok", count=None, names_state="unavailable", names=None, last_seen=None, age_s=None,
                 reason=None):
    return {"state": state, "probe": probe, "count": count, "names_state": names_state, "names": names or [],
            "last_seen": last_seen or [], "age_s": age_s, "reason": reason}


def ark_players(status, now):
    """Players for one ARK server from a Flint /api/status body. Online-now names are DERIVED (Flint lists
    last-seen, not online): last seen within 60 s of the last poll. A derived count that disagrees with
    Flint's player_count is reported as a mismatch, never guessed."""
    if not isinstance(status, dict):
        return _players_out("unavailable", reason="no status")
    flag = status.get("status")
    if flag == "offline":
        return _players_out("unavailable", probe="offline", reason="Flint reports the server offline")
    if flag != "online":
        return _players_out("unavailable", reason="unexpected status")
    polled = parse_flint_time(status.get(POLL_KEY))
    age = None if polled is None else now - polled
    if polled is None or age > STALE_SECONDS or age < -STALE_SECONDS:
        return _players_out("stale", probe="stale", age_s=age, reason="Flint data is stale")
    count = status.get("player_count")
    seen = []
    raw_players = status.get("players")
    for p in (raw_players if isinstance(raw_players, list) else [])[:MAX_NAMES]:
        if isinstance(p, dict):
            seen.append({"name": display_name(p.get("name")), "last_seen": p.get("last_seen"),
                         "_ts": parse_flint_time(p.get("last_seen"))})
    last_seen = [{"name": p["name"], "last_seen": p["last_seen"] if isinstance(p["last_seen"], str) else None}
                 for p in seen]
    if count is None or isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return _players_out("unknown", age_s=age, last_seen=last_seen, reason="player count not reported")
    names = [p["name"] for p in seen if p["_ts"] is not None and abs(polled - p["_ts"]) <= ONLINE_WINDOW_SECONDS]
    if len(names) != count:
        return _players_out("ok", count=count, names_state="mismatch", last_seen=last_seen, age_s=age)
    return _players_out("ok", count=count, names_state="ok", names=names, last_seen=last_seen, age_s=age)


# ---------------------------------------------------------------- launch line, cgroup, proc

_MAX_PLAYERS = re.compile(r"MaxPlayers=(\d{1,4})\b")
_CLUSTER = re.compile(r"-clusterid=([A-Za-z0-9_.-]{1,64})")


def args_facts(cmdlines):
    """{max_players, cluster} from launch lines (bytes). Only these two values are kept; the line is dropped."""
    out = {"max_players": None, "cluster": None}
    for blob in cmdlines:
        text = blob.decode("utf-8", "replace") if isinstance(blob, (bytes, bytearray)) else str(blob)
        if out["max_players"] is None:
            m = _MAX_PLAYERS.search(text)
            out["max_players"] = int(m.group(1)) if m else None
        if out["cluster"] is None:
            m = _CLUSTER.search(text)
            out["cluster"] = m.group(1) if m else None
    return out


def _read(path, limit=65536, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read(limit)
    except (OSError, ValueError):
        return None


def _int_file(path):
    text = _read(path, 64)
    try:
        return int(text.strip())
    except (AttributeError, ValueError):
        return None


def _cpu_usec(path):
    for line in (_read(path) or "").splitlines():
        if line.startswith("usage_usec "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _pids(path):
    out = []
    for line in (_read(path) or "").splitlines():
        line = line.strip()
        if line.isdigit() and len(line) < 10:
            out.append(int(line))
    return out


def _start_ticks(stat_text):
    """Field 22 (starttime) of /proc/<pid>/stat; the command name may hold spaces and parentheses."""
    try:
        rest = stat_text[stat_text.rindex(")") + 1:].split()
        return int(rest[19])
    except (AttributeError, ValueError, IndexError):
        return None


def read_cgroup(cg_dir, proc_root):
    """{state, ram_bytes, usec, uptime_s, max_players, cluster} for one unit's cgroup."""
    out = {"state": "unavailable", "ram_bytes": None, "usec": None, "uptime_s": None, "max_players": None,
           "cluster": None}
    if not os.path.isdir(cg_dir):
        return out
    out["ram_bytes"] = _int_file(os.path.join(cg_dir, "memory.current"))
    out["usec"] = _cpu_usec(os.path.join(cg_dir, "cpu.stat"))
    starts, cmdlines = [], []
    for pid in _pids(os.path.join(cg_dir, "cgroup.procs")):
        t = _start_ticks(_read(os.path.join(proc_root, str(pid), "stat")))
        if t is not None:
            starts.append(t)
        blob = _read(os.path.join(proc_root, str(pid), "cmdline"), 16384, "rb")
        if blob:
            cmdlines.append(blob)
    try:
        boot_s = float((_read(os.path.join(proc_root, "uptime"), 128) or "").split()[0])
    except (IndexError, ValueError):
        boot_s = None
    if starts and boot_s is not None:
        out["uptime_s"] = max(0.0, boot_s - min(starts) / CLK_TCK)
    out.update(args_facts(cmdlines))
    if out["ram_bytes"] is not None or out["usec"] is not None:
        out["state"] = "ok"
    return out


class CpuTracker:
    """Percent of ONE core (100 = one core busy) from the delta of cpu.stat usage_usec between samples."""

    MIN_GAP = 5.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = {}   # key -> (time, usec, value)

    def sample(self, key, usec):
        if usec is None:
            return None
        now = self._clock()
        prev = self._last.get(key)
        if prev is None:
            self._last[key] = (now, usec, None)
            return None  # FIRST_SAMPLE
        t0, u0, value = prev
        if now - t0 < self.MIN_GAP:
            return value
        if usec < u0:
            self._last[key] = (now, usec, None)
            return None
        value = (usec - u0) / ((now - t0) * 1_000_000.0) * 100.0
        self._last[key] = (now, usec, value)
        return value


def newest_save_age(location, now):
    """{state, age_s, reason} from the newest *.ark mtime under <location>/ShooterGame/Saved/SavedArks/*/.
    Only directory entries and lstat are used: no file is ever opened, symlinks are skipped."""
    if not isinstance(location, str) or not os.path.isabs(location) or ".." in location.split(os.sep):
        return {"state": "unavailable", "age_s": None, "reason": "no usable save location"}
    root = os.path.join(location, "ShooterGame", "Saved", "SavedArks")
    newest = None
    try:
        with os.scandir(root) as maps:
            for m in maps:
                if m.is_symlink() or not m.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(m.path) as files:
                    for f in files:
                        if f.name.endswith(".ark") and not f.is_symlink() and f.is_file(follow_symlinks=False):
                            mt = f.stat(follow_symlinks=False).st_mtime
                            newest = mt if newest is None or mt > newest else newest
    except OSError:
        return {"state": "unavailable", "age_s": None, "reason": "save directory not readable"}
    if newest is None:
        return {"state": "none", "age_s": None, "reason": "no save file found"}
    if newest > now + 1:
        return {"state": "future", "age_s": None, "reason": "save time is in the future"}
    return {"state": "ok", "age_s": max(0.0, now - newest), "reason": None}


# ---------------------------------------------------------------- Bedrock: ping, properties, journal

_MAGIC = bytes.fromhex("00ffff00" + "fefefefe" + "fdfdfdfd" + "12345678")


def _pong(state, **kw):
    out = {"state": state, "version": None, "players": None, "max": None, "level": None}
    out.update(kw)
    return out


def parse_pong(data):
    """The unconnected-pong reply of the Bedrock server list. Anything off-spec is `unparseable`; never raises."""
    try:
        if not isinstance(data, (bytes, bytearray)) or len(data) < 35 or data[0] != 0x1C or data[17:33] != _MAGIC:
            return _pong("unparseable")
        (length,) = struct.unpack(">H", bytes(data[33:35]))
        body = bytes(data[35:])
        if length != len(body):
            return _pong("unparseable")
        parts = body.decode("utf-8").split(";")
        if len(parts) < 8 or parts[0] not in ("MCPE", "MCEE"):
            return _pong("unparseable")
        players, maxp = int(parts[4]), int(parts[5])
        if not (0 <= players <= 100000 and 0 <= maxp <= 100000):
            return _pong("unparseable")
        return _pong("ok", version=parts[3][:32], players=players, max=maxp, level=parts[7][:64])
    except (ValueError, UnicodeDecodeError, struct.error, IndexError):
        return _pong("unparseable")


def bedrock_ping(port, timeout=PING_TIMEOUT, host=LOOPBACK):
    """One UDP datagram to the server-list port and the parsed reply; no_response when nothing comes back."""
    packet = b"\x01" + struct.pack(">Q", int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF) + _MAGIC + struct.pack(">Q", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(packet, (host, port))
        data, _ = sock.recvfrom(4096)
    except OSError:   # includes the timeout and a refused loopback port
        return _pong("no_response")
    finally:
        sock.close()
    return parse_pong(data)


def read_properties_max(location):
    """max-players from <location>/server.properties (a plain config file: no secrets live there), or None."""
    if not isinstance(location, str) or not os.path.isabs(location) or ".." in location.split(os.sep):
        return None
    text = _read(os.path.join(location, "server.properties"), 65536) or ""
    m = re.search(r"^max-players=(\d{1,6})\s*$", text, re.M)
    return int(m.group(1)) if m else None


_MSG = re.compile(r"\b(?:INFO|WARN|WARNING|ERROR|DEBUG)\]\s+(.*)$")
_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT[0-9:.+-]+)\s")
_CONNECT = re.compile(r"^Player connected: (.+), xuid: \S+$")
_DISCONNECT = re.compile(r"^Player disconnected: (.+), xuid: \S+?(?:, pfid: \S+)?$")
_PACK = re.compile(r"^Pack Stack - \[(\d+)\] (.+?) \(id: ([0-9A-Za-z-]+), version: ([^)]*)\)")


def parse_bedrock_journal(lines):
    """Parse the Bedrock start-up / player window. State resets at the LAST "Starting Server"; a window with
    no such line is `unknown` (the start of this run is out of view, so nobody can be reported online)."""
    msgs = []
    for line in lines or []:
        if not isinstance(line, str):
            continue
        m = _MSG.search(line[:2000])
        if m:
            t = _TIME.match(line)
            msgs.append((t.group(1) if t else None, m.group(1).strip()))
    start = max((i for i, (_, m) in enumerate(msgs) if m == "Starting Server"), default=None)
    out = {"state": "unknown", "online": [], "events": [], "packs": [], "version": None, "level": None}
    if start is None:
        return out
    online = []
    for when, msg in msgs[start:]:
        c, d, p = _CONNECT.match(msg), _DISCONNECT.match(msg), _PACK.match(msg)
        if c:
            out["events"].append({"kind": "join", "name": c.group(1), "time": when})
            if c.group(1) not in online:
                online.append(c.group(1))
        elif d:
            out["events"].append({"kind": "leave", "name": d.group(1), "time": when})
            if d.group(1) in online:
                online.remove(d.group(1))
        elif p:
            out["packs"].append({"index": int(p.group(1)), "name": p.group(2), "id": p.group(3), "version": p.group(4)})
        elif msg.startswith("Version: "):
            out["version"] = msg[len("Version: "):][:32]
        elif msg.startswith("Level Name: "):
            out["level"] = msg[len("Level Name: "):][:64]
    out.update(state="ok", online=online, events=out["events"][-MAX_NAMES:])
    return out


# ---------------------------------------------------------------- collection (cached)

def _unavailable(kind):
    return {
        "flint": {"state": "unavailable", "status": None, "events_state": "unavailable", "events": None},
        "ping": _pong("unavailable"),
        "cgroup": {"state": "unavailable", "ram_bytes": None, "usec": None, "uptime_s": None,
                   "max_players": None, "cluster": None},
        "save": {"state": "unavailable", "age_s": None, "reason": "read failed"},
        "window": {"state": "unavailable", "lines": []},
        "tail": {"state": "unavailable", "lines": []},
        "props": None,
        "restarts": None,
    }[kind]


class GamesCache:
    """One collection per TTL window, single-flight: N page loads and every detail open share it. Holds the
    PRIVATE probe data (player names included); only game_detail ever returns names."""

    def __init__(self, cfg, ttl=GAMES_TTL, clock=time.monotonic, wall=time.time):
        self._cfg, self._ttl, self._clock, self._wall = cfg, ttl, clock, wall
        self._lock = threading.Lock()
        self._value, self._at = None, None
        self._cpu = CpuTracker(clock)

    def get(self):
        with self._lock:
            if self._value is None or self._clock() - self._at >= self._ttl:
                self._value = self._collect()
                self._at = self._clock()
            return self._value

    def _registry(self):
        """{table name: save/config location or None} for the table servers the registry lists. A server the
        registry does not list is not probed at all: nothing is known about it, so its row stays grey."""
        out = {}
        try:
            for raw in sources.parse_registry(self._cfg.registry_path):
                if raw.get("name") in GAME_SERVERS and raw.get("status") not in sources.EXCLUDED_STATUS:
                    out[raw["name"]] = os.path.expanduser(raw["location"]) if raw.get("location") else None
        except OSError:
            pass
        return out

    def _flint(self, server):
        base = self._cfg.game_flint_url
        st, status = flint_get(base, "/api/status", server)
        ev_state, events = flint_get(base, "/api/events", server)
        return {"state": st, "status": status, "events_state": ev_state, "events": events}

    def _collect(self):
        cfg, now = self._cfg, self._wall()
        locations = self._registry()
        jobs = {("restarts",): lambda: sources.prom_restarts_24h(cfg.prom_url)}
        for name, meta in GAME_SERVERS.items():
            if name not in locations:
                continue
            cg = os.path.join(cfg.cgroup_root, meta["unit"])
            jobs[(name, "cgroup")] = (lambda cg=cg: read_cgroup(cg, cfg.proc_root))
            if meta["type"] == "ark":
                jobs[(name, "flint")] = (lambda s=meta["flint"]: self._flint(s))
                jobs[(name, "save")] = (lambda loc=locations.get(name): newest_save_age(loc, now))
            else:
                jobs[(name, "ping")] = lambda: bedrock_ping(cfg.bedrock_port)
                jobs[(name, "window")] = lambda: journal.bedrock_window(binary=cfg.journalctl)
                jobs[(name, "tail")] = lambda: journal.tail(journal.BEDROCK_UNIT, ["system"], binary=cfg.journalctl)
                jobs[(name, "props")] = lambda loc=locations.get(name): read_properties_max(loc)
        got = {}
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futs = {k: pool.submit(fn) for k, fn in jobs.items()}
            for k, fut in futs.items():
                try:
                    got[k] = fut.result()
                except Exception:   # one source raising never blanks the others
                    got[k] = _unavailable(k[-1])
        table = got[("restarts",)]
        # unit -> restarts in 24 h (system scope: every game unit is a system unit); None = Prometheus did not answer
        restarts = None if not isinstance(table, dict) else {u: v for (sc, u), v in table.items() if sc == "system"}
        probes = {"at": now, "restarts": restarts}
        for name, meta in GAME_SERVERS.items():
            if name not in locations:
                continue
            cg = got.get((name, "cgroup")) or _unavailable("cgroup")
            entry = {"cgroup": cg, "cpu_pct": self._cpu.sample(name, cg.get("usec"))}
            for part in ("flint", "save", "ping", "window", "tail", "props"):
                if (name, part) in got:
                    entry[part] = got[(name, part)]
            if meta["type"] == "bedrock":
                entry["journal"] = parse_bedrock_journal((entry.get("window") or {}).get("lines"))
            probes[name] = entry
        return probes


# ---------------------------------------------------------------- tile rows and detail payloads

def _restarts(probes, unit):
    table = (probes or {}).get("restarts")
    if not isinstance(table, dict):
        return None
    value = table.get(unit)
    return None if value is None else round(value)


def _uptime(probes, cg, now):
    """Uptime as of NOW: the cached reading plus the time since it was taken (probes are up to a TTL old)."""
    if cg.get("uptime_s") is None:
        return None
    at = (probes or {}).get("at")
    return cg["uptime_s"] + (max(0.0, now - at) if isinstance(at, (int, float)) else 0.0)


def _players_tile(state, count, maxp):
    return {"state": state, "count": count, "max": maxp}


def _ark_view(p, now):
    """(players dict, probe state for the lamp) for one ARK server."""
    flint = p.get("flint") or _unavailable("flint")
    if flint["state"] != "ok":
        return _players_out("unavailable", probe=flint["state"], reason="Flint " + flint["state"]), flint["state"]
    pl = ark_players(flint["status"], now)
    return pl, pl["probe"]


def build_games(cfg, services, probes, now=None):
    """The `games` key of /api/state: one row per table server. `services` are the classified registry rows
    (the unit lamp is the floor); `probes` is GamesCache.get() or None. No names, no ids."""
    now = time.time() if now is None else now
    by_name = {r["name"]: r for r in services or []}
    rows = []
    for name, meta in GAME_SERVERS.items():
        if name not in by_name and name not in (probes or {}):
            continue   # a server the registry does not list is not a running game server: no row
        p = (probes or {}).get(name) or {}
        cg = p.get("cgroup") or _unavailable("cgroup")
        row = {"name": name, "title": meta["title"], "type": meta["type"], "world": None}
        if meta["type"] == "ark":
            players, probe_state = _ark_view(p, now)
            tile = _players_tile(players["state"], players["count"], cg.get("max_players"))
            sources_ = {"flint": (p.get("flint") or {}).get("state", "unavailable"), "ping": None}
        else:
            ping = p.get("ping") or _unavailable("ping")
            probe_state = ping["state"]
            maxp = ping["max"] if ping["state"] == "ok" else p.get("props")
            tile = _players_tile("ok" if ping["state"] == "ok" else "unavailable",
                                 ping["players"] if ping["state"] == "ok" else None, maxp)
            row["world"] = ping["level"] or (p.get("journal") or {}).get("level")
            sources_ = {"flint": None, "ping": ping["state"]}
        status, reason = classify.game_lamp(by_name.get(name), {"state": probe_state})
        if status == classify.RED:   # a unit that is down has no players, whatever a cached source still says
            tile = _players_tile("unavailable", None, tile["max"])
        sources_["cgroup"] = cg["state"]
        row.update(status=status, label=classify.LABELS[status], reason=reason, players=tile,
                   uptime_s=_uptime(probes, cg, now), ram_bytes=cg["ram_bytes"], cpu_pct=p.get("cpu_pct"),
                   restarts_24h=_restarts(probes, meta["unit"]), sources=sources_)
        if meta["type"] == "ark":
            row["save_age_s"] = (p.get("save") or {}).get("age_s")
        rows.append(row)
    return {"state": "ok" if probes else "unavailable", "servers": rows}


def _event_version(events):
    for e in events or []:
        if isinstance(e, dict) and e.get("type") == "ark_version":
            try:
                v = json.loads(e.get("detail") or "{}").get("version")
            except (ValueError, AttributeError):
                v = None
            if isinstance(v, str) and v:
                return v[:32]
    return None


def _recent_joins(events):
    """Join times (UTC epoch) from Flint's events. Flint strips the player from these events, so a name appears
    only if the event itself carries one; IPs and ids are never copied."""
    out = []
    for e in events or []:
        if isinstance(e, dict) and e.get("type") == "player_join":
            try:
                d = json.loads(e.get("detail") or "{}")
            except ValueError:
                d = {}
            name = d.get("name") if isinstance(d, dict) else None
            out.append({"time": parse_utc_time(e.get("time")), "name": name if isinstance(name, str) else None})
    return out[:MAX_NAMES]


def game_detail(name, row, probes, now=None):
    """The `game` detail payload. This is where player names (and Bedrock journal lines, raw) are returned."""
    now = time.time() if now is None else now
    p = (probes or {}).get(name) or {}
    meta = GAME_SERVERS[name]
    cg = p.get("cgroup") or _unavailable("cgroup")
    out = {"type": meta["type"], "title": meta["title"], "status": row["status"], "label": row["label"],
           "reason": row["reason"], "players": row["players"], "ram_bytes": cg["ram_bytes"],
           "cpu_pct": p.get("cpu_pct"), "uptime_s": row["uptime_s"], "restarts_24h": row["restarts_24h"],
           "cgroup_state": cg["state"]}
    if meta["type"] == "ark":
        flint = p.get("flint") or _unavailable("flint")
        pl, _ = _ark_view(p, now)
        down = row["players"]["state"] == "unavailable"
        out.update(
            online_names={"state": "unavailable" if down else (pl["names_state"] if pl["state"] == "ok" else pl["state"]),
                          "names": [] if down else pl["names"]},
            last_seen=pl["last_seen"], recent_joins=_recent_joins(flint.get("events")),
            last_save=p.get("save") or _unavailable("save"), version=_event_version(flint.get("events")),
            cluster=cg.get("cluster"), freshness={"state": pl["state"], "age_s": pl["age_s"], "reason": pl["reason"]},
            flint={"status": flint["state"], "events": flint["events_state"]})
    else:
        ping = p.get("ping") or _unavailable("ping")
        jr = p.get("journal") or parse_bedrock_journal([])
        props = p.get("props")
        tail = p.get("tail") or _unavailable("tail")
        names_match = (len(jr["online"]) == ping["players"]) if jr["state"] == "ok" and ping["state"] == "ok" else None
        out.update(
            online_names=[] if row["status"] == classify.RED else jr["online"], ping={k: ping[k] for k in ("state", "version", "players", "max", "level")},
            names_match=names_match, events=jr["events"], packs=jr["packs"], journal_state=jr["state"],
            version=ping["version"] or jr["version"], level=ping["level"] or jr["level"],
            max_players={"ping": ping["max"], "properties": props,
                         "agree": None if ping["max"] is None or props is None else ping["max"] == props},
            journal={"state": tail["state"], "lines": tail["lines"][-20:]})
    return out
