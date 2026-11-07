"""Parse-only check of extension.js: node tokenises the ES module (gi:// imports are not resolved)."""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = os.path.join(ROOT, "extension", "corellia-hud-pin@corellia.local", "extension.js")
with tempfile.TemporaryDirectory() as tmp:
    dst = os.path.join(tmp, "extension.mjs")
    shutil.copy(src, dst)
    p = subprocess.run(["node", "--check", dst], capture_output=True, text=True)
if p.returncode != 0:
    print(p.stderr)
    print("EXT_SYNTAX_FAIL")
    sys.exit(1)
print("EXT_SYNTAX_OK")
