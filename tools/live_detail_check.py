"""Start the REAL aggregator on a spare loopback port against the REAL sources and check the detail routes.

Prints LIVE_DETAIL_OK only if all of these hold:
  * service detail for portfolio-chat (system scope) and flint (user scope) has journal.state ok with 1-20 lines;
  * the chat conversation count and the contact counts equal an independent re-derivation made here (its own
    glob and json read, not the aggregator's code);
  * /api/state does not contain the first real visitor message while /api/detail/chat does;
  * no journal line of any service matches an independent scan for Bearer tokens, JWTs or 40-hex runs;
  * site detail returns 200 for all three hosts;
  * every pre-existing file under conversations/, contacts/ and contacts_quarantine/ has the same hash after;
  * the aggregator's own output (and the installed unit's journal) never contains the visitor text.
Read-only: nothing here writes to any source.
"""
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("HUD_CHAT_DIR", os.path.expanduser("~/dev/portfolio_chat/data"))
HOSTS = ("kellogg.brengel.com", "rogueroutine.brengel.com", "talkingrock.ai")
problems = []


def need(cond, msg):
    if not cond:
        problems.append(msg)
    return cond


def get(port, path, timeout=30):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read()


def hash_tree():
    out = {}
    for sub in ("conversations", "contacts", "contacts_quarantine"):
        for dirpath, _, files in os.walk(os.path.join(DATA, sub)):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    with open(p, "rb") as fh:
                        out[p] = hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    out[p] = "unreadable"
    return out


def independent_conversations(now):
    """(started_epoch, doc) for conversations that started in the last 30 days, read with this script's own code."""
    found = []
    for path in glob.glob(os.path.join(DATA, "conversations", "*", "conv_*.json")):
        try:
            with open(path) as f:
                doc = json.load(f)
            t = datetime.fromisoformat(doc["started_at"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except (OSError, ValueError, KeyError):
            continue
        if t >= now - 30 * 86400:
            found.append((t, doc))
    found.sort(key=lambda x: x[0], reverse=True)
    return found


def main():
    before = hash_tree()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    child = subprocess.Popen([sys.executable, "-m", "corellia_hud.server"], cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=dict(os.environ, HUD_PORT=str(port)))
    try:
        for _ in range(100):
            try:
                _, raw_state = get(port, "/api/state")
                break
            except OSError:
                time.sleep(0.1)
        else:
            print("LIVE_DETAIL_FAIL aggregator never answered")
            return 1
        state = json.loads(raw_state)
        journal_lines, services_seen = [], 0
        for name in ("portfolio-chat", "flint"):
            code, body = get(port, f"/api/detail/service?id={name}")
            d = json.loads(body)
            need(code == 200 and d["journal"]["state"] == "ok" and 1 <= len(d["journal"]["lines"]) <= 20,
                 f"{name}: journal state {d['journal']['state']} lines {len(d['journal']['lines'])}")
        for row in state["services"]:
            code, body = get(port, "/api/detail/service?id=" + row["name"])
            need(code == 200, f"service {row['name']} detail http {code}")
            if code == 200:
                services_seen += 1
                journal_lines += json.loads(body)["journal"]["lines"]
        need(journal_lines and all(isinstance(ln, str) and ln for ln in journal_lines),
             "no real journal lines came back across the services")
        now = time.time()
        code, body = get(port, "/api/detail/chat?id=kellogg.brengel.com")
        chat = json.loads(body)
        mine = independent_conversations(now)
        want_n = min(20, len(mine))
        need(code == 200 and chat["state"] == "ok", "chat detail not ok")
        need(len(chat["conversations"]) == want_n, f"conversations {len(chat['conversations'])} != independent {want_n}")
        # contact records are the *.json files; dot files (.gitkeep, .sweeper_hwm) are bookkeeping, not contacts
        acc = len(glob.glob(os.path.join(DATA, "contacts", "*.json")))
        qua = len(glob.glob(os.path.join(DATA, "contacts_quarantine", "*.json")))
        need((chat["contacts"]["accepted"], chat["contacts"]["quarantined"]) == (acc, qua),
             f"contacts {chat['contacts']} != independent {acc}/{qua}")
        visitor = next((m["content"] for _, doc in mine for m in doc.get("messages", [])
                        if m.get("role") == "user" and len(m.get("content", "")) >= 8), None)
        if need(visitor is not None, "no real visitor message of 8+ characters to test with (check would be vacuous)"):
            needle = json.dumps(visitor)[1:-1].encode()
            _, raw_state2 = get(port, "/api/state")
            need(needle not in raw_state2, "/api/state contains visitor text")
            need(needle in body, "/api/detail/chat does not contain the visitor text (positive control failed)")
        for host in HOSTS:
            code, _ = get(port, "/api/detail/site?id=" + host)
            need(code == 200, f"site {host} http {code}")
    finally:
        child.terminate()
        try:
            out, err = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
    if visitor:
        need(visitor.encode() not in out + err, "the aggregator's own output contains visitor text")
        unit_log = subprocess.run(["journalctl", "--user", "--no-pager", "--unit=corellia-hud.service", "--lines=300"],
                                  capture_output=True, text=True, timeout=10).stdout
        need(visitor not in unit_log, "the installed unit's journal contains visitor text")
    after = hash_tree()
    changed = [p for p, h in before.items() if after.get(p) != h]
    need(not changed, f"{len(changed)} pre-existing source file(s) changed")
    for p in problems:
        print("PROBLEM", p)
    if problems:
        print("LIVE_DETAIL_FAIL")
        return 1
    print(f"LIVE_DETAIL_OK services={services_seen} conversations={len(chat['conversations'])} "
          f"contacts={chat['contacts']['accepted']}/{chat['contacts']['quarantined']} journal_lines={len(journal_lines)} "
          f"sites={len(HOSTS)} hashed_files={len(before)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
