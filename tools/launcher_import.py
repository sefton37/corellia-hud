"""Import smoke for the launcher: loads launcher/hud_window.py exactly as the unit would (same
interpreter, same gi version pins) without calling main(), so no window opens. Catches typelib
version clashes such as Gdk 4.0 loading under Gtk 3.0, which the static check cannot see."""
import importlib.util
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("hud_window", os.path.join(ROOT, "launcher", "hud_window.py"))
try:
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from gi.repository import Gdk, Gtk, WebKit2  # what the module actually bound
    versions = f"Gtk={Gtk._version} Gdk={Gdk._version} WebKit2={WebKit2._version}"
except Exception:
    traceback.print_exc()
    print("LAUNCHER_IMPORT_FAIL")
    sys.exit(1)
if (Gtk._version, Gdk._version, WebKit2._version) != ("3.0", "3.0", "4.1"):
    print(f"LAUNCHER_IMPORT_FAIL {versions}")
    sys.exit(1)
print(f"LAUNCHER_IMPORT_OK {versions}")
