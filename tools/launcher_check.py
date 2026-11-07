"""Static launcher check: no window is opened."""
import os
import re
import sys
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
launcher = open(os.path.join(ROOT, "launcher", "hud_window.py")).read()
ext = open(os.path.join(ROOT, "extension", "corellia-hud-pin@corellia.local", "extension.js")).read()

decorated = bool(re.search(r"^\s+self\.set_decorated\(False\)", launcher, re.M))
wm = re.search(r'^WM_CLASS = "([^"]+)"', launcher, re.M)
title = re.search(r'^TITLE = "([^"]+)"', launcher, re.M)
url = re.search(r'^URL = "([^"]+)"', launcher, re.M)
wm_match = bool(wm and title and f"'{wm.group(1)}'" in ext and f"'{title.group(1)}'" in ext)
loopback = bool(url and urlparse(url.group(1)).hostname == "127.0.0.1")
print(f"decorated={'false' if decorated else 'true'} wm_class_match={'yes' if wm_match else 'no'} loopback_only={'yes' if loopback else 'no'}")
if decorated and wm_match and loopback:
    print("LAUNCHER_CHECK_OK decorated=false wm_class_match=yes loopback_only=yes")
    sys.exit(0)
print("LAUNCHER_CHECK_FAIL")
sys.exit(1)
