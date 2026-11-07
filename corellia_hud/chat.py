"""portfolio-chat analytics: a counts-only summary for the HUD tile and, for the detail window, recent
conversations with their text.

Read-only by construction: conversation JSON is opened for reading only; contacts and quarantined
contacts are COUNTED from directory listings and never opened (they hold visitor emails); `ip_hash` is
never copied because every output dict is built field by field from an allowlist. Nothing here logs.
"""
import json
import os
import re
import stat
import sqlite3
from datetime import datetime, timedelta, timezone

from . import sources

WINDOW_DAYS = 30
MAX_CONVERSATIONS = 20
MAX_MESSAGES = 40
MAX_MESSAGE_CHARS = 4000
MAX_FILE_BYTES = 1_000_000
TRUNC_MARK = " [truncated]"
CHAT_SITE = "kellogg"          # analytics site key of the site that hosts the chat widget
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_ts(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cut(text):
    text = text if isinstance(text, str) else ""
    if len(text) > MAX_MESSAGE_CHARS:
        return text[:MAX_MESSAGE_CHARS] + TRUNC_MARK, True
    return text, False


def _shape(doc, started):
    """The allowlisted view of one conversation document (no ip_hash, no unknown keys)."""
    msgs = doc.get("messages") if isinstance(doc.get("messages"), list) else []
    out = []
    for m in msgs[:MAX_MESSAGES]:
        if not isinstance(m, dict):
            continue
        content, cut = _cut(m.get("content"))
        row = {"role": m.get("role") if m.get("role") in ("user", "assistant") else "other",
               "content": content, "truncated": cut, "timestamp": str(m.get("timestamp") or "")}
        if isinstance(m.get("domain"), str):
            row["domain"] = m["domain"][:60]
        if _num(m.get("response_time_ms")) is not None:
            row["response_time_ms"] = _num(m["response_time_ms"])
        out.append(row)
    domains = doc.get("domains_used") if isinstance(doc.get("domains_used"), list) else []
    blocked = doc.get("blocked_at_layer")
    return {
        "id": str(doc.get("id") or "")[:64], "started_at": str(doc.get("started_at") or ""),
        "started_epoch": started, "last_activity": str(doc.get("last_activity") or ""),
        "total_turns": int(_num(doc.get("total_turns")) or 0),
        "domains_used": [str(d)[:60] for d in domains[:10]],
        "total_response_time_ms": _num(doc.get("total_response_time_ms")),
        "blocked_at_layer": None if blocked is None else str(blocked)[:60],
        "messages": out, "messages_truncated": len(msgs) > MAX_MESSAGES,
    }


def _read_json(path):
    """The parsed document, or None for anything that is not a small regular file holding a JSON object."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as f:
            doc = json.loads(f.read(MAX_FILE_BYTES + 1))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def scan(data_dir, now, days=WINDOW_DAYS):
    """(state, conversations newest-first, skipped) for conversations that STARTED within `days`."""
    root = os.path.join(data_dir, "conversations")
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return "missing", [], 0
    except OSError:
        return "unreachable", [], 0
    cutoff = now - days * 86400
    first_day = (datetime.fromtimestamp(cutoff, tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    last_day = (datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    convs, skipped = [], 0
    for name in names:
        day = os.path.join(root, name)
        if not _DATE_DIR.match(name) or os.path.islink(day) or not os.path.isdir(day):
            skipped += 1
            continue
        if not first_day <= name <= last_day:
            continue
        try:
            files = sorted(f for f in os.listdir(day) if f.startswith("conv_") and f.endswith(".json"))
        except OSError:
            skipped += 1
            continue
        for fname in files:
            doc = _read_json(os.path.join(day, fname))
            started = _parse_ts(doc.get("started_at")) if doc else None
            if doc is None or started is None:
                skipped += 1
                continue
            if started >= cutoff:
                convs.append(_shape(doc, started))
    convs.sort(key=lambda c: c["started_epoch"], reverse=True)
    return "ok", convs, skipped


def count_files(path):
    """(state, n) from a directory listing alone: the files are never opened."""
    try:
        n = sum(1 for e in os.scandir(path) if not e.name.startswith(".") and e.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreachable", None
    return "ok", n


def contacts(data_dir):
    sa, accepted = count_files(os.path.join(data_dir, "contacts"))
    sq, quarantined = count_files(os.path.join(data_dir, "contacts_quarantine"))
    state = "ok" if sa == sq == "ok" else next(s for s in (sa, sq) if s != "ok")
    return {"state": state, "accepted": accepted, "quarantined": quarantined}


def funnel(analytics_db, now, days=7):
    """chat_open -> chat_consent over `days` from daily_interactions. `recorded` is False when no consent
    row exists at all: zero then means "none recorded", never a measured conversion rate."""
    if not os.path.exists(analytics_db):
        return {"state": "missing", "opens": None, "consents": None, "recorded": False}
    since = (datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        con = sources.open_ro(analytics_db)
        try:
            rows = con.execute(
                "SELECT kind, SUM(count), COUNT(*) FROM daily_interactions WHERE site = ? AND date >= ? "
                "AND kind IN ('chat_open', 'chat_consent') GROUP BY kind", (CHAT_SITE, since)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {"state": "unreachable", "opens": None, "consents": None, "recorded": False}
    by = {k: (int(total or 0), rows_n) for k, total, rows_n in rows}
    return {"state": "ok", "opens": by.get("chat_open", (0, 0))[0], "consents": by.get("chat_consent", (0, 0))[0],
            "recorded": by.get("chat_consent", (0, 0))[1] > 0}


def _summarise(state, convs, now, data_dir, analytics_db):
    day, week = now - 86400, now - 7 * 86400
    in7 = [c for c in convs if c["started_epoch"] >= week]
    turns = sum(c["total_turns"] for c in in7)
    resp = sum(c["total_response_time_ms"] or 0.0 for c in in7)
    return {
        "state": state,
        "conversations_24h": sum(1 for c in in7 if c["started_epoch"] >= day),
        "conversations_7d": len(in7),
        "turns_7d": turns,
        "blocked_7d": sum(1 for c in in7 if c["blocked_at_layer"] is not None),
        "avg_response_ms": round(resp / turns, 1) if turns else None,
        "funnel": funnel(analytics_db, now),
        "contacts": contacts(data_dir),
    }


def chat_summary(data_dir, analytics_db, now):
    """The counts-only tile payload. Never contains a message, an ip_hash or a contact detail."""
    state, convs, _skipped = scan(data_dir, now)
    return _summarise(state, convs, now, data_dir, analytics_db)


def chat_detail(data_dir, analytics_db, now):
    """The detail-window payload: the summary plus up to 20 recent conversations with their text."""
    state, convs, skipped = scan(data_dir, now)
    summary = _summarise(state, convs, now, data_dir, analytics_db)
    shown = [{k: v for k, v in c.items() if k != "started_epoch"} for c in convs[:MAX_CONVERSATIONS]]
    return {"state": state, "summary": summary, "funnel": summary["funnel"], "contacts": summary["contacts"],
            "conversations": shown, "skipped": skipped, "window_days": WINDOW_DAYS}
