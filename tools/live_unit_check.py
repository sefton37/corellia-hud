"""The INSTALLED unit on 127.0.0.1:8670 serves the detail routes, and the tailnet address still refuses."""
import json
import socket
import subprocess
import sys
import urllib.request

PORT = 8670
base = f"http://127.0.0.1:{PORT}"
routes = loopback = csp = False
try:
    with urllib.request.urlopen(base + "/api/detail/services?id=green", timeout=20) as r:
        d = json.load(r)
        header = r.headers.get("Content-Security-Policy", "")
    with urllib.request.urlopen(base + "/detail.html?kind=services&id=green", timeout=5) as r:
        page = r.read()
    routes = d.get("kind") == "services" and b"Corellia detail" in page
    csp = "object-src 'none'" in header and "frame-ancestors 'none'" in header
    with urllib.request.urlopen(base + "/api/state", timeout=20) as r:
        loopback = set(json.load(r)) == {"meta", "alerts", "services", "perf", "web", "games", "claude"}
except Exception as e:
    print("loopback probe failed:", type(e).__name__)
tail = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.split()
tail_ip = tail[0] if tail else "100.91.184.86"
try:
    socket.create_connection((tail_ip, PORT), timeout=3).close()
    refused = False
except ConnectionRefusedError:
    refused = True
except OSError as e:
    print("tailnet probe inconclusive:", type(e).__name__)
    refused = False
yn = lambda b: "yes" if b else "no"  # noqa: E731
print(f"detail_routes={yn(routes)} loopback={yn(loopback)} tailnet_refused={yn(refused)} csp={yn(csp)}")
if routes and loopback and refused and csp:
    print(f"LIVE_UNIT_OK port={PORT} detail_routes=yes loopback=yes tailnet_refused=yes csp=yes")
    sys.exit(0)
print("LIVE_UNIT_FAIL")
sys.exit(1)
