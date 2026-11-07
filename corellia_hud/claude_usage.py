"""Claude usage from the local transcripts: how many tokens were burned, by whom, and when.

Local files only: this module has no network and reads no credentials. It reads the Claude Code transcript
tree (read-only, symlinks never followed) and keeps, per assistant turn, only
(timestamp, model, input, output, cache_write, cache_read, project basename, session id).
Prompt and response text never enters any structure here.

Vocabulary (the headline is NEW tokens):
  new tokens   = input + cache_creation + output
  cache read   = re-read from the prompt cache; reported separately, never inside `new`
  processed    = new + cache read (detail window only)
  cache hit    = cache_read / (cache_read + cache_creation + input); None when the denominator is 0

Duplicates: one API response is written as several transcript lines. The dedupe key is
(message id, request id), global across files (a resumed session spans two); the copy with the LARGEST
output_tokens wins (the last line is not always the largest). Subagent files are included.

Scanning is incremental: each file keeps (inode, size, offset); only whole lines are consumed, so a partial
trailing line waits; a file that shrinks, changes inode or vanishes has its turns dropped and is rescanned.
Windows are cut on the LOCAL calendar (transcript timestamps are UTC), so DST days are 23 or 25 hours long.
History is the transcripts' retention (about a month): a window longer than the history is labelled partial
with the real coverage start, and a day before the history has no value rather than a zero.
"""
import json
import os
import re
import stat
import threading
import time
from datetime import datetime, timedelta, timezone

USAGE_TTL = 15.0               # seconds, monotonic clock: the minimum gap between two scans
MAX_LINE = 4 * 1024 * 1024     # a longer line is skipped and counted
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
NAME_CAP = 80
FUTURE_SLACK = 86400           # a turn stamped more than a day ahead of the wall clock is not believed
HOUR = 3600
DAY = 86400
DETAIL_IDS = ("usage", "models", "projects")
TOP_MODELS = 4
TOP_PROJECTS = 20
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# turn tuple layout
TS, MODEL, INP, OUT, CW, CR, PROJECT, SESSION = range(8)


def _count(v):
    """A non-negative int that is not a bool, or None."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _epoch(text):
    """UTC (or offset) ISO timestamp -> epoch seconds, or None."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _project(cwd):
    if not isinstance(cwd, str):
        return "unknown"
    base = os.path.basename(cwd.rstrip("/"))
    return (base or "unknown")[:NAME_CAP]


def local_midnight(epoch):
    d = datetime.fromtimestamp(epoch)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _local_hour_start(epoch):
    return datetime.fromtimestamp(epoch).replace(minute=0, second=0, microsecond=0).timestamp()


def _new(t):
    return t[INP] + t[CW] + t[OUT]


def _agg(turns):
    inp = sum(t[INP] for t in turns)
    out = sum(t[OUT] for t in turns)
    cw = sum(t[CW] for t in turns)
    cr = sum(t[CR] for t in turns)
    return {"turns": len(turns), "input": inp, "output": out, "cache_write": cw, "cache_read": cr,
            "new": inp + cw + out, "processed": inp + cw + out + cr}


def _hit_rate(a):
    denom = a["cache_read"] + a["cache_write"] + a["input"]
    return None if denom == 0 else a["cache_read"] / denom


class _File:
    __slots__ = ("inode", "size", "offset", "turns")

    def __init__(self):
        self.inode = self.size = None
        self.offset = 0
        self.turns = {}   # dedupe key -> turn tuple (this file's copy only)


class ClaudeUsageIndex:
    def __init__(self, root, ttl=USAGE_TTL, clock=time.monotonic, wall=time.time):
        self._root, self._ttl, self._clock, self._wall = root, max(float(ttl), USAGE_TTL), clock, wall
        self._lock = threading.RLock()
        self._files = {}           # path -> _File
        self._at = None            # monotonic time of the last scan
        self._merged_cache = None
        self._exists = None
        self._stats = {"files": 0, "bytes_read_total": 0, "bytes_read_last": 0, "full_rescans": 0, "bad_lines": 0,
                       "skipped": 0, "unreadable": 0}

    # ------------------------------------------------------------------ scanning
    def refresh(self, force=False):
        """Scan when the TTL has elapsed (or force). Never raises."""
        with self._lock:
            if not force and self._at is not None and self._clock() - self._at < self._ttl:
                return
            try:
                self._scan()
            except Exception:   # a scan bug must degrade to the last good index, never to an exception
                pass
            self._at = self._clock()

    def _scan(self):
        st = self._stats
        st["bytes_read_last"] = 0
        st["unreadable"] = 0
        self._exists = os.path.isdir(self._root)
        seen = set()
        if self._exists:
            for dirpath, dirnames, filenames in os.walk(self._root, followlinks=False):
                dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                for name in filenames:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(dirpath, name)
                    if os.path.islink(path):
                        continue
                    seen.add(path)
                    self._scan_file(path)
        for path in [p for p in self._files if p not in seen]:   # vanished
            del self._files[path]
            self._merged_cache = None
        st["files"] = len(self._files)

    def _scan_file(self, path):
        st = self._stats
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            st["unreadable"] += 1
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return
            rec = self._files.get(path)
            if rec is None:
                rec = self._files[path] = _File()
            elif rec.inode != info.st_ino or info.st_size < rec.offset:
                st["full_rescans"] += 1
                rec.turns, rec.offset = {}, 0
                self._merged_cache = None
            rec.inode = info.st_ino
            if info.st_size == rec.offset:
                rec.size = info.st_size
                return
            with os.fdopen(fd, "rb", closefd=False) as f:
                self._consume(f, rec)
            rec.size = info.st_size
        except OSError:
            st["unreadable"] += 1
        finally:
            os.close(fd)

    def _consume(self, f, rec):
        st = self._stats
        f.seek(rec.offset)
        pos = rec.offset
        start = pos
        while True:
            raw = f.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE and not raw.endswith(b"\n"):   # oversize: skip to its newline, or wait for it
                n, complete = len(raw), False
                while True:
                    chunk = f.readline(1 << 20)
                    n += len(chunk)
                    if not chunk:
                        break
                    if chunk.endswith(b"\n"):
                        complete = True
                        break
                if not complete:
                    break
                pos += n
                st["skipped"] += 1
                continue
            if not raw.endswith(b"\n"):   # a partial trailing line waits for its newline
                break
            pos += len(raw)
            self._line(raw, rec)
        rec.offset = pos
        st["bytes_read_last"] += pos - start
        st["bytes_read_total"] += pos - start
        self._merged_cache = None

    def _line(self, raw, rec):
        st = self._stats
        if b'"usage"' not in raw:   # cheap prefilter: most lines carry no usage at all
            return
        try:
            obj = json.loads(raw)
        except ValueError:
            st["bad_lines"] += 1
            return
        if not isinstance(obj, dict):
            st["bad_lines"] += 1
            return
        msg = obj.get("message")
        if not isinstance(msg, dict) or "usage" not in msg:
            return
        usage = msg["usage"]
        if not isinstance(usage, dict):
            st["bad_lines"] += 1
            return
        counts = [_count(usage.get(k)) for k in TOKEN_FIELDS]
        ts = _epoch(obj.get("timestamp"))
        if None in counts or ts is None or ts > self._wall() + FUTURE_SLACK:
            st["bad_lines"] += 1
            return
        model = msg.get("model")
        if model == "<synthetic>":
            st["skipped"] += 1
            return
        if not isinstance(model, str) or not MODEL_RE.match(model):
            model = "other"
        mid, rid, uid = msg.get("id"), obj.get("requestId"), obj.get("uuid")
        if isinstance(mid, str) and isinstance(rid, str) and mid and rid:
            key = (mid, rid)
        elif isinstance(uid, str) and uid:
            key = ("uuid", uid)
        else:
            st["skipped"] += 1
            return
        session = obj.get("sessionId")
        session = session[:NAME_CAP] if isinstance(session, str) and session else "unknown"
        turn = (ts, model, counts[0], counts[1], counts[2], counts[3], _project(obj.get("cwd")), session)
        old = rec.turns.get(key)
        if old is None or turn[OUT] > old[OUT]:
            rec.turns[key] = turn

    def _merged(self):
        """key -> the best (largest output) turn across every file; rebuilt only after a change."""
        with self._lock:
            if self._merged_cache is None:
                out = {}
                for rec in self._files.values():
                    for key, turn in rec.turns.items():
                        old = out.get(key)
                        if old is None or turn[OUT] > old[OUT]:
                            out[key] = turn
                self._merged_cache = out
            return self._merged_cache

    # ------------------------------------------------------------------ views
    def _state(self, turns):
        if not self._exists:
            return "missing"
        return "ok" if turns else "empty"

    def _scan_block(self):
        return dict(self._stats)

    def _windows(self, turns, now):
        """Window bounds (start, end) in epoch seconds, on the local calendar."""
        midnight = local_midnight(now)
        return {"today": (midnight, now), "h24": (now - DAY, now), "d7": (now - 7 * DAY, now),
                "d30": (now - 30 * DAY, now)}

    @staticmethod
    def _in(turns, win):
        lo, hi = win
        return [t for t in turns if lo <= t[TS] <= hi]

    def _models(self, turns):
        by = {}
        for t in turns:
            by[t[MODEL]] = by.get(t[MODEL], 0) + _new(t)
        total = sum(by.values())
        rows = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
        out = [{"model": m, "new": n, "share": (n / total if total else None)} for m, n in rows[:TOP_MODELS]]
        rest = sum(n for _, n in rows[TOP_MODELS:])
        if rest:
            out.append({"model": "other", "new": rest, "share": rest / total if total else None})
        return out

    def summary(self, now=None):
        """The tile: windows today / 24 h / 7 d, hit rate, burn, model shares, sessions, scan counts."""
        now = self._wall() if now is None else now
        self.refresh()
        with self._lock:
            turns = list(self._merged().values())
            state = self._state(turns)
            out = {"state": state, "windows": None, "cache_hit_rate": None, "burn": None, "models": [],
                   "sessions_today": None, "coverage_since": None, "scan": self._scan_block()}
            if state != "ok":
                return out
            wins = self._windows(turns, now)
            since = min(t[TS] for t in turns)
            w7 = _agg(self._in(turns, wins["d7"]))
            out["windows"] = {}
            for k in ("today", "h24", "d7"):
                a = _agg(self._in(turns, wins[k]))
                a["partial"] = k == "d7" and wins[k][0] < since
                out["windows"][k] = a
            out["cache_hit_rate"] = _hit_rate(w7)
            hour = _agg(self._in(turns, (now - HOUR, now)))
            out["burn"] = {"last_60m_new": hour["new"], "hourly_avg_24h": out["windows"]["h24"]["new"] / 24}
            out["models"] = self._models(self._in(turns, wins["d7"]))
            out["sessions_today"] = len({t[SESSION] for t in self._in(turns, wins["today"])})
            out["coverage_since"] = since
            return out

    def _peak_hour(self, turns, now, since):
        if now - since < DAY:
            return None
        by = {}
        for t in turns:
            h = datetime.fromtimestamp(t[TS]).hour
            by[h] = by.get(h, 0) + _new(t)
        if not by:
            return None
        hour = max(sorted(by), key=lambda h: by[h])
        return {"hour": hour, "new": by[hour]}

    def _longest_session(self, turns):
        spans = {}
        for t in turns:
            lo, hi, n = spans.get(t[SESSION], (t[TS], t[TS], 0))
            spans[t[SESSION]] = (min(lo, t[TS]), max(hi, t[TS]), n + 1)
        if not spans:
            return None
        lo, hi, n = max(spans.values(), key=lambda s: (s[1] - s[0], s[2]))
        return {"seconds": hi - lo, "turns": n, "started_at": lo, "label": "wall-clock span"}

    def _hourly(self, turns, now, since):
        top = _local_hour_start(now)
        edges = [top - k * HOUR for k in range(23, -1, -1)]
        floor = _local_hour_start(since)
        out = []
        for lo in edges:
            if lo < floor:
                out.append({"start": lo, "new": None, "cache_read": None})
                continue
            a = _agg([t for t in turns if lo <= t[TS] < lo + HOUR])
            out.append({"start": lo, "new": a["new"], "cache_read": a["cache_read"]})
        return out

    def _daily(self, turns, now, since):
        today = datetime.fromtimestamp(now).date()
        floor = local_midnight(since)
        out = []
        for back in range(29, -1, -1):
            day = today - timedelta(days=back)
            lo = datetime(day.year, day.month, day.day).timestamp()
            nxt = day + timedelta(days=1)
            hi = datetime(nxt.year, nxt.month, nxt.day).timestamp()
            if lo < floor:
                out.append({"date": day.isoformat(), "new": None, "cache_read": None, "turns": None})
                continue
            a = _agg([t for t in turns if lo <= t[TS] < hi])
            out.append({"date": day.isoformat(), "new": a["new"], "cache_read": a["cache_read"], "turns": a["turns"]})
        return out

    def detail(self, ident, now=None):
        """The payload behind a tile figure, or None for an id that is not one of DETAIL_IDS."""
        if ident not in DETAIL_IDS:
            return None
        now = self._wall() if now is None else now
        self.refresh()
        with self._lock:
            turns = list(self._merged().values())
            state = self._state(turns)
            base = {"state": state, "scan": self._scan_block(), "coverage_since": None, "coverage_date": None}
            if state != "ok":
                return base
            since = min(t[TS] for t in turns)
            base["coverage_since"] = since
            base["coverage_date"] = datetime.fromtimestamp(since).strftime("%Y-%m-%d")
            wins = self._windows(turns, now)
            if ident == "usage":
                base["windows"] = {}
                for k in ("today", "h24", "d7", "d30"):
                    a = _agg(self._in(turns, wins[k]))
                    a["partial"] = k in ("d7", "d30") and wins[k][0] < since
                    base["windows"][k] = a
                base["cache_hit_rate"] = {k: _hit_rate(v) for k, v in base["windows"].items()}
                base["burn"] = {"last_60m_new": _agg(self._in(turns, (now - HOUR, now)))["new"],
                                "hourly_avg_24h": base["windows"]["h24"]["new"] / 24}
                base["sessions_today"] = len({t[SESSION] for t in self._in(turns, wins["today"])})
                base["longest_session"] = self._longest_session(turns)
                base["peak_hour"] = self._peak_hour(turns, now, since)
                base["hourly"] = self._hourly(turns, now, since)
                base["daily"] = self._daily(turns, now, since)
                return base
            win = self._in(turns, wins["d7"])
            base["window"] = "d7"
            base["partial"] = wins["d7"][0] < since
            if ident == "models":
                rows = {}
                for t in win:
                    rows.setdefault(t[MODEL], []).append(t)
                total = sum(_new(t) for t in win)
                base["rows"] = sorted(
                    ({"model": m, **_agg(ts), "share": (_agg(ts)["new"] / total if total else None)}
                     for m, ts in rows.items()), key=lambda r: (-r["new"], r["model"]))
                return base
            rows = {}
            for t in win:
                rows.setdefault(t[PROJECT], []).append(t)
            out = []
            for name, ts in rows.items():
                a = _agg(ts)
                out.append({"project": name, "sessions": len({t[SESSION] for t in ts}), **a})
            out.sort(key=lambda r: (-r["new"], r["project"]))
            base["rows"] = out[:TOP_PROJECTS]
            base["projects_total"] = len(out)
            return base
