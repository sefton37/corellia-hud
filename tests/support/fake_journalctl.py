#!/usr/bin/env python3
"""A fake `journalctl` for tests and the e2e run. It never reads a real journal.

Behaviour comes from the environment so the same script serves unit tests and the e2e stack:
  FAKE_JOURNAL_MODE     ok (default) | denied | empty | slow
  FAKE_JOURNAL_LOG      file that receives one JSON argv line per invocation (proves "no call")
  FAKE_JOURNAL_HOSTILE  set to 1 to include markup payloads in the output
It deliberately emits 30 lines whatever `--lines` says, so the 20-line cap is proven to be applied by
the caller. The last line and one middle line carry a sentinel secret (the positive control that the
secret is returned verbatim).
"""
import json
import os
import sys
import time

SECRET = "JRNL5ECRET7x9Kq2Lm4Pz8Vw3Bn6"


def main():
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_JOURNAL_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(sys.argv) + "\n")
    mode = os.environ.get("FAKE_JOURNAL_MODE", "ok")
    unit = next((a.split("=", 1)[1] for a in argv if a.startswith("--unit=")), "?")
    scope = "user" if "--user" in argv else "system"
    # FAKE_JOURNAL_FILE: serve the lines of that file like a real journal would: --grep (regex) filters,
    # --lines=N keeps the last N. Used for the Bedrock journal; unset, the canned 30 lines below apply.
    src = os.environ.get("FAKE_JOURNAL_FILE")
    if src and mode == "ok":
        import re
        with open(src) as f:
            lines = f.read().splitlines()
        pat = next((a.split("=", 1)[1] for a in argv if a.startswith("--grep=")), None)
        if pat:
            lines = [ln for ln in lines if re.search(pat, ln)]
        n = int(next((a.split("=", 1)[1] for a in argv if a.startswith("--lines=")), "100000"))
        print("\n".join(lines[-n:]))
        return 0
    if mode == "denied":
        sys.stderr.write("No journal files were opened due to insufficient permissions.\n")
        return 1
    if mode == "empty":
        print("-- No entries --")
        return 0
    if mode == "slow":
        time.sleep(30)
    print(f"-- Journal begins at Mon 2026-11-02 --")
    for i in range(1, 31):
        text = f"line {i:02d} scope={scope}"
        if i == 25:
            text += f" Authorization: Bearer {SECRET}"
        if i == 28 and os.environ.get("FAKE_JOURNAL_HOSTILE"):
            text += ' <img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script> <a href="javascript:window.__pwned=1">x</a> "><svg onload=window.__pwned=1>'
        if i == 30:
            text += f" token={SECRET}"
        print(f"2026-11-05T12:00:{i:02d}-06:00 corellia {unit}[123]: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
