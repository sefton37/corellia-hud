"""Start the REAL aggregator on a spare loopback port and compare its /api/state to live Prometheus."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROM = os.environ.get("HUD_PROM_URL", "http://100.91.184.86:9090")
CF_STATES = {"token_needed", "token_insecure", "ok", "cf_error", "unreachable"}


def prom(q):
    url = f"{PROM}/api/v1/query?{urllib.parse.urlencode({'query': q})}"
    return json.load(urllib.request.urlopen(url, timeout=5))["data"]["result"]


def main():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    child = subprocess.Popen([sys.executable, "-m", "corellia_hud.server"], cwd=ROOT, stdout=subprocess.DEVNULL,
                             env=dict(os.environ, HUD_PORT=str(port)))
    try:
        for _ in range(50):
            try:
                st = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=10))
                break
            except OSError:
                time.sleep(0.1)
        else:
            print("CROSSCHECK_FAIL aggregator never answered")
            return 1
    finally:
        child.terminate()
    by = {r["name"]: r for r in st["services"]}
    errors = []
    if st["meta"]["sources"]["prometheus"] != "ok":
        errors.append("prometheus source state: " + st["meta"]["sources"]["prometheus"])
    checked = 0
    for r in prom("corellia_service_port_up"):
        name, val = r["metric"]["service"], float(r["value"][1])
        row = by.get(name)
        if row is None and name == "corellia-hud":
            continue  # the HUD excludes itself from its own list (spec #481); Prometheus now probes it
        if row is None:
            errors.append(f"{name}: port_up series has no service row")
        elif val == 0 and row["status"] != "red":
            errors.append(f"{name}: port_up 0 but {row['status']}")
        elif val == 1 and row["status"] != "green" and not (row["status"] == "amber" and (row["stale"] or "restart" in row["reason"])):
            errors.append(f"{name}: port_up 1 but {row['status']} ({row['reason']})")
        checked += 1
    # unit-mapped services agree with corellia_unit_active in the registry's own scope
    active = {(r["metric"]["scope"], r["metric"]["unit"]): float(r["value"][1]) for r in prom("corellia_unit_active")}
    reg = {}
    import re
    for name, kind, unit in re.findall(r"^  ([a-z0-9-]+):\n    kind: (systemd(?:-user)?)\n(?:.*\n)*?    unit: ([a-z0-9@.-]+\.service)", open(
            os.path.expanduser("~/.claude/ops/registry.yaml")).read(), re.M):
        reg[name] = ("system" if kind == "systemd" else "user", unit)
    unit_checked = 0
    for name, key in reg.items():
        if name in by and key in active and name not in ("corellia-hud",):
            expect = "green" if active[key] == 1 else "red"
            if by[name]["status"] != expect and by[name]["status"] != "amber":
                errors.append(f"{name}: unit_active{key}={active[key]} but {by[name]['status']}")
            unit_checked += 1
    if st["meta"]["sources"]["cloudflare"] not in CF_STATES:
        errors.append("cloudflare state not an enum value")
    for e in errors:
        print("MISMATCH", e)
    if errors:
        print("CROSSCHECK_FAIL")
        return 1
    print(f"port_up_checked={checked} unit_checked={unit_checked} cloudflare={st['meta']['sources']['cloudflare']}")
    print(f"CROSSCHECK_OK prometheus=ok services={len(st['services'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
