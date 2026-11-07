"""Games section against the REAL sources.

Default mode: start the real aggregator on a spare loopback port and prints LIVE_GAMES_OK only if ALL hold:
  * 4 servers, named as the registry's game units; each lamp agrees with an independent `systemctl is-active`;
  * each ARK player count equals an independent read of Flint /api/status; the Bedrock count equals an
    independent UDP server-list ping (retried to ride out the 30 s cache);
  * max players equals an independent read of the process argument (ARK) / server.properties (Bedrock);
  * RAM within 5% of systemd's MemoryCurrent; uptime within 10 s of ActiveEnterTimestampMonotonic;
  * CPU (second sample, 31 s later) is a number within 10 points of this script's own cpu.stat delta;
  * last-save age within 120 s of an independent newest-*.ark scan;
  * /api/state contains no real player name, no xuid, no 32-hex run, and neither the ARK admin secret nor Flint's
    credentials (read here only to prove their absence; never printed); /api/detail/game for an ARK server DOES
    contain a real last-seen name (positive control);
  * Flint's three source files are unchanged; the aggregator uses < 10% of one core over 40 s of polling; its own
    output contains no player name.
`--unit`: the INSTALLED sandboxed unit on :8670 answers with 4 servers whose cgroup / save / Flint sources are
readable from inside the sandbox, and the tailnet address refuses.
Read-only: nothing here writes to any source.
"""
import glob
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
SERVERS = {"ark-server": "Extinction", "ark-astraeos": "Astraeos", "ark-genesis": "Genesis", "bedrock": None}
FLINT = "http://127.0.0.1:8090"
FLINT_SRC = [HOME + "/dev/Flint/src/flint/" + p for p in ("web/routes.py", "collectors/rcon.py", "config.py")]
MAGIC = bytes.fromhex("00ffff00" + "fefefefe" + "fdfdfdfd" + "12345678")
problems = []


def need(cond, msg):
    if not cond:
        problems.append(msg)
    return cond


def get(port, path, timeout=30):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read()


def sysd(unit, prop):
    out = subprocess.run(["systemctl", "show", unit, "-p", prop, "--value"], capture_output=True, text=True).stdout
    return out.strip()


def cg(unit):
    return f"/sys/fs/cgroup/system.slice/{unit}.service"


def cpu_usec(unit):
    for line in open(cg(unit) + "/cpu.stat"):
        if line.startswith("usage_usec "):
            return int(line.split()[1])
    return None


def launch_lines(unit):
    out = []
    for pid in open(cg(unit) + "/cgroup.procs").read().split():
        try:
            out.append(open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace"))
        except OSError:
            pass
    return out


def ping():
    for _ in range(3):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
            s.sendto(b"\x01" + struct.pack(">Q", 1) + MAGIC + struct.pack(">Q", 2), ("127.0.0.1", 19132))
            data, _ = s.recvfrom(4096)
            parts = data[35:].decode().split(";")
            return int(parts[4]), int(parts[5])
        except (OSError, ValueError, IndexError):
            time.sleep(1)
        finally:
            s.close()
    return None, None


def flint(server, path="/api/status"):
    with urllib.request.urlopen(f"{FLINT}{path}?server={server}", timeout=5) as r:
        return json.load(r)


def secrets():
    """Admin secrets, read only to prove they never appear in any output. Never printed."""
    found = set()
    for server, line in ((u, l) for u in ("ark-server", "ark-astraeos", "ark-genesis") for l in launch_lines(u)):
        for m in re.finditer(r"Server" + r"Admin" + r"Pass" + r"word=([^?\s\0]+)", line):
            found.add(m.group(1))
    try:
        text = open(HOME + "/.flint/servers.toml").read()
        for m in re.finditer(r'(?im)^\s*\w*pass\w*\s*=\s*"([^"]+)"', text):
            found.add(m.group(1))
    except OSError:
        pass
    return {s for s in found if len(s) >= 4}


def file_hashes(paths):
    return {p: hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths}


def newest_save(location):
    best = None
    for f in glob.glob(os.path.join(location, "ShooterGame/Saved/SavedArks/*/*.ark")):
        if not os.path.islink(f):
            best = max(best or 0, os.lstat(f).st_mtime)
    return best


def proc_cpu(pid):
    parts = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")


def unit_mode():
    port = 8670
    try:
        _, raw = get(port, "/api/state", 20)
        g = json.loads(raw)["games"]
    except (OSError, KeyError, ValueError) as e:
        print("installed unit probe failed:", type(e).__name__)
        print("LIVE_GAMES_UNIT_FAIL")
        return 1
    rows = {r["name"]: r for r in g["servers"]}
    need(set(rows) == set(SERVERS), "servers " + str(sorted(rows)))
    for n, r in rows.items():
        need(r["sources"]["cgroup"] == "ok" and r["ram_bytes"] and r["uptime_s"] is not None, f"{n}: cgroup not readable from the sandbox")
        if SERVERS[n]:
            need(r["sources"]["flint"] == "ok", f"{n}: flint source {r['sources']['flint']}")
            need(r.get("save_age_s") is not None, f"{n}: save age not readable from the sandbox")
        else:
            need(r["sources"]["ping"] == "ok", f"bedrock: ping {r['sources']['ping']}")
    tail = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.split()
    try:
        socket.create_connection((tail[0] if tail else "100.91.184.86", port), timeout=3).close()
        loop = False
    except ConnectionRefusedError:
        loop = True
    except OSError:
        loop = False
    need(loop, "the tailnet address did not refuse")
    for p in problems:
        print("PROBLEM", p)
    if problems:
        print("LIVE_GAMES_UNIT_FAIL")
        return 1
    print(f"LIVE_GAMES_UNIT_OK port={port} servers={len(rows)} loopback=yes")
    return 0


def main():
    if "--unit" in sys.argv:
        return unit_mode()
    before_src = file_hashes(FLINT_SRC)
    secret_values = secrets()
    port = 18000 + os.getpid() % 1000
    env = dict(os.environ, HUD_PORT=str(port), HUD_TTL="1")
    proc = subprocess.Popen([sys.executable, "-m", "corellia_hud.server"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_text, names = "", set()
    try:
        for _ in range(60):
            try:
                get(port, "/api/state", 20)
                break
            except OSError:
                time.sleep(0.5)
        cpu0_time, cpu0 = time.monotonic(), {u: cpu_usec(u) for u in SERVERS}
        agg0 = proc_cpu(proc.pid)
        first = json.loads(get(port, "/api/state")[1])["games"]
        need(all(r["cpu_pct"] is None for r in first["servers"]), "first CPU sample was not n/a")
        t_end = time.monotonic() + 40
        raw = b""
        while time.monotonic() < t_end:       # 40 s of polling; the games cache lapses once (>= 30 s)
            raw = get(port, "/api/state")[1]
            time.sleep(5)
        cpu1_time, cpu1 = time.monotonic(), {u: cpu_usec(u) for u in SERVERS}
        agg1 = proc_cpu(proc.pid)
        snap = json.loads(raw)
        rows = {r["name"]: r for r in snap["games"]["servers"]}
        need(set(rows) == set(SERVERS), "servers " + str(sorted(rows)))
        for n, flint_name in SERVERS.items():
            r = rows[n]
            active = sysd(n + ".service", "ActiveState")
            need((active == "active") == (r["status"] in ("green", "amber")), f"{n}: lamp {r['status']} vs systemd {active}")
            ram = int(sysd(n + ".service", "MemoryCurrent") or 0)
            need(ram and abs(r["ram_bytes"] - ram) <= 0.05 * ram, f"{n}: ram {r['ram_bytes']} vs {ram}")
            mono = int(sysd(n + ".service", "ActiveEnterTimestampMonotonic") or 0) / 1e6
            up = float(open("/proc/uptime").read().split()[0]) - mono
            need(r["uptime_s"] is not None and abs(r["uptime_s"] - up) <= 10, f"{n}: uptime {r['uptime_s']} vs {up}")
            own = (cpu1[n] - cpu0[n]) / ((cpu1_time - cpu0_time) * 1e6) * 100
            need(r["cpu_pct"] is not None and abs(r["cpu_pct"] - own) <= 10, f"{n}: cpu {r['cpu_pct']} vs {own:.1f}")
            if flint_name:
                st = flint(flint_name)
                names |= {p["name"] for p in st.get("players", []) if not re.fullmatch(r"[0-9a-f]{8}", p["name"] or "")}
                if st.get("status") == "online" and st.get("player_count") is not None:
                    need(r["players"]["count"] == st["player_count"], f"{n}: players {r['players']} vs flint {st['player_count']}")
                mp = [int(m.group(1)) for l in launch_lines(n) for m in [re.search(r"MaxPlayers=(\d+)", l)] if m]
                need(mp and r["players"]["max"] == mp[0], f"{n}: max {r['players']['max']} vs args {mp}")
                reg = re.search(rf"(?ms)^  {n}:.*?^    location: (\S+)", open(HOME + "/.claude/ops/registry.yaml").read())
                loc = os.path.expanduser(reg.group(1)) if reg else None
                newest = newest_save(loc) if loc else None
                need(newest is not None and r["save_age_s"] is not None and abs(r["save_age_s"] - (time.time() - newest)) <= 120,
                     f"{n}: save age {r.get('save_age_s')}")
            else:
                count, maxp = ping()
                need(count is not None and r["players"]["count"] == count and r["players"]["max"] == maxp, f"bedrock: {r['players']} vs ping {count}/{maxp}")
                props = open(HOME + "/bedrock_server/server.properties").read()
                need(maxp == int(re.search(r"(?m)^max-players=(\d+)", props).group(1)), "bedrock ping max != server.properties")
        text = raw.decode()
        for name in names:
            need(name not in text, "a real player name is in /api/state")
        need("xuid" not in text.lower() and not re.search(r"\b[0-9a-f]{32}\b", text), "xuid or a 32-hex run in /api/state")
        for s in secret_values:
            need(s not in text, "an admin secret is in /api/state")
        need(bool(secret_values), "control: no admin secret could be read to test for")
        status, body = get(port, "/api/detail/game?id=ark-server")
        pool = [p["name"] for p in flint("Extinction").get("players", []) if not re.fullmatch(r"[0-9a-f]{8}", p["name"] or "")]
        need(status == 200 and pool and any(p in body.decode() for p in pool), "detail window lacks a real last-seen name")
        for s in secret_values:
            need(s not in body.decode(), "an admin secret is in the game detail")
        need(file_hashes(FLINT_SRC) == before_src, "a Flint source file changed")
        used = (agg1 - agg0) / (cpu1_time - cpu0_time) * 100
        need(used < 10, f"aggregator used {used:.1f}% of a core")
        print(f"aggregator_cpu={used:.2f}% names_checked={len(names)}")
    finally:
        proc.terminate()
        try:
            out_text = proc.communicate(timeout=5)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
    for name in names:
        need(name not in out_text, "a player name is in the aggregator's own output")
    for p in problems:
        print("PROBLEM", p)
    if problems:
        print("LIVE_GAMES_FAIL")
        return 1
    print("LIVE_GAMES_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
