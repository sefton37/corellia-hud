"""Install: extension copy, units linked + enabled (aggregator started, window NOT), gsettings entry."""
import ast
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
uuid = sys.argv[1]
src = os.path.join(ROOT, "extension", uuid)
dst = os.path.expanduser(f"~/.local/share/gnome-shell/extensions/{uuid}")
if os.path.isdir(dst):
    shutil.rmtree(dst)
shutil.copytree(src, dst)


def run(*cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(" ".join(cmd), "->", p.stderr.strip())
        sys.exit(1)
    return p.stdout.strip()


units = [os.path.join(ROOT, "systemd", n) for n in ("corellia-hud.service", "corellia-hud-window.service")]
run("systemctl", "--user", "link", *units)
run("systemctl", "--user", "daemon-reload")
run("systemctl", "--user", "enable", "--now", "corellia-hud.service")      # aggregator: running
run("systemctl", "--user", "restart", "corellia-hud.service")              # an updated install must serve the new routes
run("systemctl", "--user", "enable", "corellia-hud-window.service")        # window: first start is the next login

cur = run("gsettings", "get", "org.gnome.shell", "enabled-extensions")
cur = cur[len("@as "):] if cur.startswith("@as ") else cur
items = ast.literal_eval(cur)
if uuid not in items:
    run("gsettings", "set", "org.gnome.shell", "enabled-extensions", repr(items + [uuid]))
print("installed: extension copy, units enabled, aggregator restarted; the window unit is NOT started here.")
print("The detail-window placement lives in the extension: log out and back in once for the shell to load it.")
