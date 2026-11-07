"""The installed unit answers on loopback and refuses the tailnet address."""
import json
import socket
import subprocess
import sys
import urllib.request

PORT = 8670
try:
    state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=15))
    page = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5).read()
    loopback = set(state) == {"meta", "alerts", "services", "perf", "web", "games", "claude"} and b"Corellia" in page
except OSError as e:
    print("loopback probe failed:", type(e).__name__)
    loopback = False
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
print(f"loopback={'yes' if loopback else 'no'} tailnet_refused={'yes' if refused else 'no'}")
if loopback and refused:
    print(f"SERVICE_CHECK_OK port={PORT} loopback=yes tailnet_refused=yes")
    sys.exit(0)
print("SERVICE_CHECK_FAIL")
sys.exit(1)
