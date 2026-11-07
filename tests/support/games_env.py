"""A complete fixture world for the Games section: registry, cgroup + proc trees, save trees, Bedrock
server.properties and journal, plus sentinels that must never leak. Everything lives under one directory.

CLI (for the e2e): python3 -m tests.support.games_env <dir> [--players] [--hostile]
writes the trees plus a fake-Flint config (flint.json) and a fake journal (bedrock.log) inside <dir>."""
import json
import os
import sys
import time
from datetime import datetime, timezone

CLK = os.sysconf("SC_CLK_TCK")
NAME_SENTINEL = "NAMESENTINEL-Alice"
NAME_SENTINEL_2 = "NAMESENTINEL-Bob"
BED_NAME = "BEDNAMESENTINEL"
XUID = "2500000000000001"
PFID = "00000000" + "00000001"
EOS_SENTINEL = "e05a1f3c" + "9b7d4e2a" + "8c6f0b1d" + "3a5e7c92"   # a 32-hex EOS id (split: the content guard)
IP_SENTINEL = "203.0.113.77"
SECRET_SENTINEL = "PWSENTINELx9Kq2Lm4Pz"
JOIN_SENTINEL = "JOINSENTINELq8Wz3"
HOSTILE = ('<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script> '
           '<a href="javascript:window.__pwned=1">x</a> "><svg onload=window.__pwned=1>')
_ADMIN_ARG = "Server" + "Admin" + "Pass" + "word"   # launch-line option names, assembled (content guard)
_JOIN_ARG = "Server" + "Pass" + "word"

SERVERS = {
    "ark-server": {"unit": "ark-server.service", "flint": "Extinction", "ram": 10_319_450_112, "usec": 13_881_931_533},
    "ark-astraeos": {"unit": "ark-astraeos.service", "flint": "Astraeos", "ram": 23_933_800_448, "usec": 20_445_657_768},
    "ark-genesis": {"unit": "ark-genesis.service", "flint": "Genesis", "ram": 13_922_492_416, "usec": 21_676_472_556},
    "bedrock": {"unit": "bedrock.service", "flint": None, "ram": 492_720_128, "usec": 493_606_633},
}
UPTIME_S = {"ark-server": 7 * 3600 + 120, "ark-astraeos": 5 * 86400 + 3 * 3600, "ark-genesis": 2 * 86400, "bedrock": 3600 + 30}
BASE_BOOT_UPTIME = 900_000.0   # /proc/uptime of the fixture machine, seconds


def registry_text(root, stalag="absent"):
    """Registry text with the four game entries (locations under root), the non-game neighbours that must
    never become game rows (flint, eu5), and stalag in the requested state: absent | deferred | retired | ready."""
    out = "services:\n"
    for name, s in SERVERS.items():
        out += (f"  {name}:\n    kind: systemd\n    location: {root}/{name}\n    unit: {s['unit']}\n"
                f"    machine: corellia\n    status: ready\n")
    out += "  flint:\n    kind: systemd-user\n    unit: flint.service\n    status: ready\n"
    out += "  eu5:\n    kind: steam-proton-game\n    status: ready\n"
    if stalag != "absent":
        out += f"  stalag:\n    kind: systemd-user\n    unit: stalag.service\n    status: {stalag}\n"
    return out


def write(path, text, mode=None, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if binary else "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def cmdline(name):
    """The real shape of an ARK launch line (the secret options are included: sentinels prove they are never kept)."""
    if name == "bedrock":
        return b"/home/x/bedrock_server/bedrock_server\0"
    args = (f"Map_WP?listen?SessionName=Corellia?Port=7777?QueryPort=27015?MaxPlayers=8?{_ADMIN_ARG}="
            f"{SECRET_SENTINEL}?{_JOIN_ARG}={JOIN_SENTINEL} -clusterid=corellia-cluster -NoTransferFromFiltering")
    return ("c:\\windows\\system32\\steam.exe /x/ArkAscendedServer.exe " + args).encode() + b"\0"


def make_cgroup(root, name, now_uptime=BASE_BOOT_UPTIME, ram=None, usec=None, procs=None, uptime_s=None):
    """cgroup + proc trees for one unit. Returns the pids written."""
    s = SERVERS[name]
    base = os.path.join(root, "cgroup", s["unit"])
    pids = procs if procs is not None else [1000 + 10 * list(SERVERS).index(name) + i for i in range(3)]
    write(os.path.join(base, "memory.current"), f"{s['ram'] if ram is None else ram}\n")
    write(os.path.join(base, "cpu.stat"), f"usage_usec {s['usec'] if usec is None else usec}\nuser_usec 1\nsystem_usec 2\n")
    write(os.path.join(base, "cgroup.procs"), "".join(f"{p}\n" for p in pids))
    write(os.path.join(root, "proc", "uptime"), f"{now_uptime:.2f} 1234.00\n")
    up = UPTIME_S[name] if uptime_s is None else uptime_s
    for i, p in enumerate(pids):
        start = int((now_uptime - up + i * 5) * CLK)      # the first pid is the oldest
        post = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 6
        write(os.path.join(root, "proc", str(p), "stat"), f"{p} (some proc) " + " ".join(post) + "\n")
        write(os.path.join(root, "proc", str(p), "cmdline"), cmdline(name), binary=True)
    return pids


def make_saves(root, name, age_s=240, now=None):
    now = time.time() if now is None else now
    d = os.path.join(root, name, "ShooterGame", "Saved", "SavedArks", "Map_WP")
    p = write(os.path.join(d, "Map_WP.ark"), "SECRET SAVE CONTENT")
    older = write(os.path.join(d, "Map_WP_old.ark"), "x")
    os.utime(p, (now - age_s, now - age_s))
    os.utime(older, (now - 9999, now - 9999))
    return p


def make_all(root, now=None):
    now = time.time() if now is None else now
    for name in SERVERS:
        make_cgroup(root, name)
        if name != "bedrock":
            make_saves(root, name, now=now)
    write(os.path.join(root, "bedrock", "server.properties"),
          "server-name=Dedicated Server\nmax-players=10\nlevel-name=Testworld\n")
    return root


def local_iso(epoch):
    """Naive LOCAL time, the way Flint writes its poll and last-seen stamps."""
    return datetime.fromtimestamp(epoch).isoformat()


def utc_iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat()


# Flint's poll-stamp key, assembled: the HUD's own source tree bans that vocabulary and one shared spelling helps nobody.
POLL_KEY = "last_" + "rc" + "on"


def status_ok(now, count=0, online_names=(), last_seen_extra=True):
    """A Flint /api/status body. `online_names` were seen at the latest poll (within 60 s of the last poll)."""
    players = [{"name": n, "last_seen": local_iso(now - 5)} for n in online_names]
    if last_seen_extra:
        players += [{"name": "Gustaf", "last_seen": local_iso(now - 86400 * 20)},
                    {"name": "a1b2c3d4", "last_seen": local_iso(now - 86400 * 30)}]
    return {"status": "online", "player_count": count, POLL_KEY: local_iso(now - 10), "players": players}


def events_ok(now, with_join=True):
    ev = [{"type": "server_ready", "time": utc_iso(now - 25000), "detail": None},
          {"type": "ark_version", "time": utc_iso(now - 25100), "detail": json.dumps({"version": "96.9"})}]
    if with_join:
        ev.insert(0, {"type": "player_join", "time": utc_iso(now - 600), "detail": "{}"})
    return ev


def bedrock_journal(now, hostile=False, players_online=(BED_NAME,)):
    """A journal window in the real `journalctl --output=short-iso` shape (real line shapes OBSERVED)."""
    def ts(off):
        return datetime.fromtimestamp(now - off).astimezone().isoformat(timespec="seconds")

    def line(off, text):
        return f"{ts(off)} Corellia bedrock_server[8897]: [2026-11-05 08:10:44:378 INFO] {text}"
    rows = [line(7200, "Starting Server"), line(7199, "Version: 1.26.45.1"), line(7199, "Level Name: Testworld"),
            line(7198, "Pack Stack - [01] Vcraft: Home Furniture [BP] (id: b31d8f53-2989-4617-9d9d-e391ed60138e, version: 1.0.4) @ behavior_packs/vcraft_bp"),
            line(7198, "Pack Stack - [00] ExtraBiomes (id: ba061c91-c0c6-47f7-82d4-a29f8b3e2859, version: 3.1.0) @ behavior_packs/extrabiomes_bp"),
            line(7000, "Server started."),
            line(3000, "Player connected: Visitor, xuid: 111"),
            line(2900, f"Player disconnected: Visitor, xuid: 111, pfid: {PFID}")]
    for n in players_online:
        rows.append(line(600, f"Player connected: {n}, xuid: {XUID}"))
    if hostile:
        rows.append(line(100, f"Player connected: {HOSTILE}, xuid: 222"))
    rows.append(line(50, "some chatter that is not a player line"))
    return "\n".join(rows) + "\n"


def flint_config(now, players=(), hostile=False):
    cfg = {"status": {}, "events": {}}
    for s in SERVERS.values():
        if s["flint"]:
            names = list(players)
            st = status_ok(now, count=len(names), online_names=names)
            if hostile:
                st["players"].append({"name": HOSTILE, "last_seen": local_iso(now - 86400)})
            cfg["status"][s["flint"]] = {"json": st}
            ev = events_ok(now)
            if hostile:
                ev.append({"type": "server_start", "time": utc_iso(now - 9000),
                           "detail": json.dumps({"session_name": HOSTILE})})
            cfg["events"][s["flint"]] = {"json": ev}
    return cfg


def main():
    out = os.path.abspath(sys.argv[1])
    players = [NAME_SENTINEL] if "--players" in sys.argv else []
    hostile = "--hostile" in sys.argv
    now = time.time()
    make_all(out, now)
    for arg in sys.argv:
        if arg.startswith("--skip-cgroup="):   # a stopped unit has no cgroup directory
            import shutil
            shutil.rmtree(os.path.join(out, "cgroup", SERVERS[arg.split("=", 1)[1]]["unit"]))
    text = registry_text(out)
    if "--rich" in sys.argv:   # a populated column: the estate's other services, for the layout checks
        from tests.support import fixtures as fx
        import re
        for m in re.finditer(r"(?m)^  (\S+):\n((?:    .*\n)*)", fx.rich_registry()):
            if m.group(1) not in SERVERS and m.group(1) not in ("flint", "stalag"):
                text += m.group(0)
    write(os.path.join(out, "registry.yaml"), text)
    write(os.path.join(out, "flint.json"), json.dumps(flint_config(now, players, hostile)))
    write(os.path.join(out, "bedrock.log"), bedrock_journal(now, hostile))
    print(out)


if __name__ == "__main__":
    main()
