"""How much of the 5-hour and weekly Claude limits is used, from Anthropic's OAuth usage endpoint.

This module is the ONLY place that opens the Claude Code credentials file and the ONLY place that names the
one outbound host. Kel's binding decision (spec 485): the HUD reads the Claude Code access token and makes GET
requests to exactly https://api.anthropic.com/api/oauth/usage, the call Claude Code's own usage screen makes.

Hardening:
  * fixed host, path and scheme constants; nothing in the environment or the config can change them, and any
    other target is refused before an opener is touched;
  * GET only, no body, no query, redirects refused (a 3xx is a state, not a hop), the default TLS context
    (certificate and hostname verified), proxies ignored, the bearer set as an unredirected header;
  * body capped, short timeout, only the four window keys read and from each only utilization / resets_at;
  * one request per TTL at most, single-flight, on a worker thread (state building never waits on the network),
    failure backoff, Retry-After honoured and capped;
  * the credentials file is opened O_RDONLY|O_NOFOLLOW, must be a regular file of ours with no group or other
    bits, is capped in size, and is re-read on every attempt. Only the access token, its expiry and its scopes
    are taken out of it and the parsed object is dropped on the spot. The token lives in one local variable for
    one request: never on self, never in a payload, an error, a repr or a message.
  * the HUD never renews a token. Claude Code owns its credentials; an expired token is the explicit state
    `token_expired` and recovers by itself once Claude Code has rewritten the file.
Kill switch: HUD_CLAUDE_LIMITS=0 disables the file read and the request; the state is `disabled`.
"""
import json
import math
import os
import re
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LIMITS_HOST = "api.anthropic.com"
LIMITS_PATH = "/api/oauth/usage"
LIMITS_TTL = 300
LIMITS_TIMEOUT = 4.0
LIMITS_MAX_BODY = 65536
LIMITS_URL = "https://" + LIMITS_HOST + LIMITS_PATH
MIN_TTL = 120                  # a configured TTL below this is raised to it
FRESH_GRACE = 30               # a value this far past the TTL still reads as current while the next fetch runs
CRED_MAX = 65536
BACKOFF = (60, 120, 300, 900)  # consecutive-failure ladder, seconds
RETRY_AFTER_CAP = 3600
REAUTH_WAIT = 900              # after a 401: no retry for this long unless the credentials file changes
BETA = "oauth-2025-04-20"
# The endpoint's `utilization` is a percent already (0-100): the multiplier that turns it into one. Applied in one place.
UTIL_SCALE = 1.0
MAX_PCT = 1000.0
PROFILE_SCOPE = "user:profile"
WINDOW_KEYS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
ALWAYS_KEYS = ("five_hour", "seven_day")

STATES = ("ok", "stale", "pending", "disabled", "no_credentials", "cred_insecure", "cred_unreadable",
          "cred_malformed", "token_expired", "needs_reauth", "scope_missing", "rate_limited", "http_error", "timeout",
          "unreachable", "bad_json", "too_large", "schema_mismatch")
# States where the next attempt is allowed as soon as the credentials file changes.
FILE_STATES = frozenset({"no_credentials", "cred_insecure", "cred_unreadable", "cred_malformed", "token_expired",
                         "needs_reauth", "scope_missing"})


def default_cred_path():
    return os.environ.get("HUD_CLAUDE_CRED") or os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")


def _disabled():
    return os.environ.get("HUD_CLAUDE_LIMITS") == "0"


# ---------------------------------------------------------------- credentials (read-only, projected)

def read_credentials(path, wall=time.time):
    """(state, access token, expiry in epoch ms, scopes). State is `ok` or one of the credential states; on any
    state but ok the other three are None. Only those three fields leave the parsed object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "no_credentials", None, None, None
    except OSError as e:
        return ("cred_insecure" if e.errno == 40 else "cred_unreadable"), None, None, None   # 40 = ELOOP: a symlink
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return "cred_insecure", None, None, None
        if info.st_size > CRED_MAX:
            return "cred_malformed", None, None, None
        raw = b""
        while len(raw) <= CRED_MAX:
            chunk = os.read(fd, CRED_MAX + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return "cred_unreadable", None, None, None
    finally:
        os.close(fd)
    if len(raw) > CRED_MAX:
        return "cred_malformed", None, None, None
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return "cred_malformed", None, None, None
    del raw
    oauth = obj.get("claudeAiOauth") if isinstance(obj, dict) else None
    if not isinstance(oauth, dict):
        return "cred_malformed", None, None, None
    token, expires, scopes = oauth.get("accessToken"), oauth.get("expiresAt"), oauth.get("scopes")
    del obj, oauth
    if not isinstance(token, str) or not token or len(token) > 8192:
        return "cred_malformed", None, None, None
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
            return "cred_malformed", None, None, None
        expires = int(expires)
        if expires <= wall() * 1000:
            return "token_expired", None, None, None
    if scopes is not None:
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            return "cred_malformed", None, None, None
        if PROFILE_SCOPE not in scopes:
            return "scope_missing", None, None, None
    return "ok", token, expires, scopes


# ---------------------------------------------------------------- the one outbound request

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # a 3xx is reported as a state, never followed


def _build_opener():
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _request(opener, url, token):
    """(kind, payload, extra): ok -> (raw bytes, None) | http -> (status, headers) | refused | timeout |
    unreachable | too_large. Any URL but the constant is refused before the opener is touched."""
    if url != LIMITS_URL:
        return "refused", None, None
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json", "User-Agent": "corellia-hud", "anthropic-beta": BETA})
    req.add_unredirected_header("Authorization", "Bearer " + token)
    try:
        with opener.open(req, timeout=LIMITS_TIMEOUT) as r:
            raw = r.read(LIMITS_MAX_BODY + 1)
            status = getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        headers = e.headers
        e.close()
        return "http", e.code, headers
    except TimeoutError:
        return "timeout", None, None
    except urllib.error.URLError as e:
        return ("timeout" if isinstance(e.reason, TimeoutError) else "unreachable"), None, None
    except OSError:
        return "unreachable", None, None
    if len(raw) > LIMITS_MAX_BODY:
        return "too_large", None, None
    if status != 200:
        return "http", status, None
    return "ok", raw, None


def _retry_after(headers):
    """Seconds from a Retry-After header: an integer 1..cap, or None (absent, negative, a date, nonsense)."""
    try:
        text = str(headers.get("Retry-After")).strip() if headers is not None else ""
    except Exception:
        return None
    if not re.fullmatch(r"\d{1,12}", text):
        return None
    return min(max(int(text), 1), RETRY_AFTER_CAP)


# ---------------------------------------------------------------- parsing

def _parse_reset(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()


def _parse_windows(obj):
    """Raw windows {key: {state, pct, resets_at}} or None when the object has none of the window keys."""
    if not isinstance(obj, dict) or not any(k in obj for k in WINDOW_KEYS):
        return None
    out = {}
    for key in WINDOW_KEYS:
        if key not in obj and key not in ALWAYS_KEYS:
            continue
        w = obj.get(key)
        if w is None:   # OBSERVED live: an idle 5-hour window is a JSON null, not an object
            if key in ALWAYS_KEYS:
                out[key] = {"state": "none", "pct": None, "resets_at": None}
            continue   # a null per-model window is simply not shown
        if not isinstance(w, dict):
            out[key] = {"state": "bad_value", "pct": None, "resets_at": None}
            continue
        util, resets = w.get("utilization"), _parse_reset(w.get("resets_at"))
        if util is None:
            out[key] = {"state": "none", "pct": None, "resets_at": None}
        elif isinstance(util, bool) or not isinstance(util, (int, float)) or not math.isfinite(util) or util < 0:
            out[key] = {"state": "bad_value", "pct": None, "resets_at": None}
        else:
            pct = float(util) * UTIL_SCALE
            if pct > MAX_PCT:
                out[key] = {"state": "bad_value", "pct": None, "resets_at": None}
            else:
                out[key] = {"state": "ok", "pct": pct, "resets_at": resets}
    return out


def _windows_at(raw, now):
    """Public windows at wall time `now`: a window whose reset has passed is withheld as stale_window."""
    out = {}
    for key, w in raw.items():
        pub = {"state": w["state"], "pct": None, "left_pct": None, "resets_at": None, "resets_in_s": None, "reset": None}
        if w["state"] == "ok":
            resets = w["resets_at"]
            if resets is not None and resets <= now:
                pub["state"] = "stale_window"
            else:
                pub["pct"] = round(w["pct"], 2)
                pub["left_pct"] = round(max(0.0, 100.0 - w["pct"]), 2)
                pub["reset"] = "known" if resets is not None else "unknown"
                if resets is not None:
                    pub["resets_at"] = resets
                    pub["resets_in_s"] = int(resets - now)
        out[key] = pub
    return out


def parse_limits(obj, now):
    """(state, windows): `ok` with the public windows at `now`, or `schema_mismatch` with None."""
    raw = _parse_windows(obj)
    return ("ok", _windows_at(raw, now)) if raw is not None else ("schema_mismatch", None)


# ---------------------------------------------------------------- the cache

class LimitsCache:
    """Stale-while-revalidate: get() returns the current snapshot at once and starts at most one worker when a
    fetch is due. It never blocks on the network and never raises."""

    def __init__(self, cred_path, ttl=LIMITS_TTL, opener=None, clock=time.monotonic, wall=time.time):
        self._path, self._ttl = cred_path, max(float(ttl), MIN_TTL)
        self._opener = opener if opener is not None else _build_opener()
        self._clock, self._wall = clock, wall
        self._cv = threading.Condition()
        self._inflight = False
        self._last = None          # state of the last attempt (None before the first)
        self._good = None          # {"raw": windows, "at": monotonic}
        self._next_at = None
        self._fails = 0
        self._stamp = None
        self._fetches = 0

    # ---- public
    def get(self, diag=False, fetch=True):
        """The snapshot. `fetch=False` never starts a worker (a detail window reads without triggering a fetch)."""
        with self._cv:
            if _disabled():
                snap = self._snap("disabled", None, None, None)
            else:
                if fetch and self._due():
                    self._inflight = True
                    threading.Thread(target=self._run, daemon=True).start()
                snap = self._view()
            if diag:
                nxt = None if self._next_at is None else max(0, round(self._next_at - self._clock()))
                snap["diag"] = {"fetch_count": self._fetches, "ttl_s": self._ttl, "failures": self._fails,
                                "next_attempt_in_s": nxt, "last_state": self._last}
            return snap

    def wait_idle(self, timeout=5.0):
        """Block until no fetch is running (for tests); True when idle."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._inflight, timeout)

    # ---- scheduling
    def _stat(self):
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _due(self):
        if self._inflight:
            return False
        if self._next_at is None or self._clock() >= self._next_at:
            return True
        return self._last in FILE_STATES and self._stat() != self._stamp

    def _run(self):
        try:
            result = self._attempt()
        except Exception:   # nothing the worker meets may escape: an explicit state instead
            result = ("unreachable", None, None)
        with self._cv:
            self._finish(*result)
            self._inflight = False
            self._cv.notify_all()

    def _attempt(self):
        """(state, raw windows or None, retry seconds or None). The token exists only inside this frame."""
        self._stamp = self._stat()
        state, token, _expires, _scopes = read_credentials(self._path, self._wall)
        if state != "ok":
            return state, None, None
        self._fetches += 1
        kind, payload, extra = _request(self._opener, LIMITS_URL, token)
        token = None
        if kind == "http":
            if payload == 401:
                return "needs_reauth", None, None
            if payload == 403:
                return "scope_missing", None, None
            if payload == 429:
                return "rate_limited", None, _retry_after(extra)
            return "http_error", None, None
        if kind != "ok":
            return ("unreachable" if kind == "refused" else kind), None, None
        try:
            obj = json.loads(payload)
        except (ValueError, RecursionError):
            return "bad_json", None, None
        raw = _parse_windows(obj)
        if raw is None:
            return "schema_mismatch", None, None
        return "ok", raw, None

    def _finish(self, state, raw, retry):
        now = self._clock()
        self._last = state
        if state == "ok":
            self._good = {"raw": raw, "at": now}
            self._fails = 0
            wait = self._ttl
        elif state == "needs_reauth":
            wait = REAUTH_WAIT
        elif state in FILE_STATES:
            wait = self._ttl
        else:
            wait = retry if (state == "rate_limited" and retry is not None) else BACKOFF[min(self._fails, len(BACKOFF) - 1)]
            self._fails += 1
        self._next_at = now + wait

    # ---- the snapshot
    @staticmethod
    def _snap(state, reason, age, windows, retry=None):
        return {"state": state, "reason": reason, "age_s": age, "retry_in_s": retry, "windows": windows}

    def _view(self):
        now, wall = self._clock(), self._wall()
        good, last = self._good, self._last
        age = None if good is None else max(0, int(now - good["at"]))
        retry = None if self._next_at is None or last == "ok" else max(0, round(self._next_at - now))
        if last is None:
            return self._snap("pending", None, None, None)
        if last == "ok":
            if now - good["at"] <= self._ttl + FRESH_GRACE:
                return self._snap("ok", None, age, _windows_at(good["raw"], wall))
            if now - good["at"] <= 2 * self._ttl:
                return self._snap("stale", "refresh_pending", age, _windows_at(good["raw"], wall))
            return self._snap("pending", None, None, None)
        if good is not None and now - good["at"] <= 2 * self._ttl:
            return self._snap("stale", last, age, _windows_at(good["raw"], wall), retry)
        return self._snap(last, None, None, None, retry)
