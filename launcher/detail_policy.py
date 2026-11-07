"""Which URIs may open (or re-target) the detail window. Pure: no gi, no GTK, unit-testable.

The HUD column never navigates; a stat is a link with target="corellia-hud-detail", WebKit reports it as a
new-window request, and the launcher asks this module whether to honour it. Anything but the aggregator's
own detail page, with a well-formed `kind` and `id`, is refused.
"""
import re
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
PORT = 8670
PATH = "/detail.html"
KINDS = ("service", "services", "perf", "site", "chat", "alert", "game")
MAX_URI = 512
_NETLOC = f"{HOST}:{PORT}"
_BAD_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_ID = re.compile(r"^[A-Za-z0-9@:._-]{1,120}\Z")


def allowed(uri):
    """True only for http://127.0.0.1:8670/detail.html?kind=<kind>&id=<id> (and nothing extra)."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI or _BAD_CHARS.search(uri):
        return False
    try:
        parts = urlsplit(uri)
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query))
    except ValueError:
        return False
    if parts.scheme != "http" or parts.netloc != _NETLOC or parts.path != PATH:
        return False
    if set(query) != {"kind", "id"} or len(query["kind"]) != 1 or len(query["id"]) != 1:
        return False
    return query["kind"][0] in KINDS and bool(_ID.match(query["id"][0]))
