"""Source-tree checks that fence the Claude usage KPI's secret read and its one outbound call (spec 485).

  python3 tools/check_claude_secrets.py <secret|network|logging|readonly>   -> CLAUDE_SECRETS_OK group=<g>
  python3 tools/check_claude_secrets.py --selftest                          -> SELFTEST_CAUGHT n/14

The self-test plants each of 14 violations in a scratch copy of the tree and requires the matching group to
fail on it (and to pass on the clean copy), so a green group is not vacuous."""
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRED_NAME = ".credentials" + ".json"   # assembled: this tool is not the module the check is about
TWO = ("claude_usage.py", "claude_limits.py")
LIMITS = "claude_limits.py"
ALLOWED_HOSTS = {"api.cloudflare.com", "api.anthropic.com"}
ANTHROPIC = "api.anthropic" + ".com"
PAGES = ("hud.js", "detail.js", "index.html", "detail.html")


def read(root, rel):
    with open(os.path.join(root, rel)) as f:
        return f.read()


def pkg_files(root):
    d = os.path.join(root, "corellia_hud")
    return sorted(n for n in os.listdir(d) if n.endswith(".py"))


def two(root):
    return {n: read(root, "corellia_hud/" + n) for n in TWO}


def grep(pattern, text, flags=0):
    return [m for m in re.finditer(pattern, text, flags)]


def check_secret(root):
    bad = []
    holders = [n for n in pkg_files(root) if CRED_NAME in read(root, "corellia_hud/" + n)]
    if holders != [LIMITS]:
        bad.append(f"credentials filename referenced by {holders}, want only {LIMITS}")
    refresh = r"refreshToken|refresh_token|/oauth/token|grant_type"
    for n in pkg_files(root):
        if grep(refresh, read(root, "corellia_hud/" + n)):
            bad.append(f"refresh vocabulary in corellia_hud/{n}")
    for n in PAGES:
        if grep(r"accessToken|Bearer|Authorization", read(root, "static/" + n)):
            bad.append(f"credential word in static/{n}")
    for line in read(root, "corellia_hud/" + LIMITS).splitlines():
        if "os.environ" in line and not re.search(r"HUD_CLAUDE_(LIMITS|CRED)\b", line):
            bad.append("os.environ used for something other than HUD_CLAUDE_LIMITS / HUD_CLAUDE_CRED")
    for n in TWO:
        if "os.environ" in read(root, "corellia_hud/" + n) and n != LIMITS:
            bad.append(f"os.environ in {n}")
        if grep(r"self\.\w*(token|access|bearer)\w*\s*=", read(root, "corellia_hud/" + n), re.I):
            bad.append(f"a token-like attribute is assigned on self in {n}")
    return bad


def check_network(root):
    bad = []
    for n in pkg_files(root):
        text = read(root, "corellia_hud/" + n)
        if ANTHROPIC in text and n != LIMITS:
            bad.append(f"the host literal appears in {n}")
        for m in grep(r"https://([A-Za-z0-9.-]+)", text):
            if m.group(1) not in ALLOWED_HOSTS:
                bad.append(f"https literal for a third host in {n}: {m.group(1)}")
    limits = read(root, "corellia_hud/" + LIMITS)
    if grep(r"http://", limits):
        bad.append("an http:// literal in claude_limits.py")
    for n, text in two(root).items():
        for pat, why in ((r"CERT_NONE", "CERT_NONE"), (r"check_hostname\s*=\s*False", "check_hostname off"),
                         (r"_create_unverified_context|_create_default_https_context", "unverified context"),
                         (r"verify\s*=\s*False", "verify=False"), (r"method\s*=\s*[\"'](?!GET[\"'])", "a non-GET method"),
                         (r"\bdata\s*=", "a data= argument"), (r"POST", "a POST"),
                         (r"^\s*(import socket|from socket)", "a socket import")):
            if grep(pat, text, re.M):
                bad.append(f"{why} in {n}")
    if "O_NOFOLLOW" not in limits:
        bad.append("O_NOFOLLOW missing")
    if not grep(r"^CRED_MAX\s*=\s*\d+", limits, re.M) or "CRED_MAX" not in limits.split("CRED_MAX", 1)[1]:
        bad.append("credentials size cap missing")
    if not grep(r"def redirect_request\([^)]*\):\s*\n(?:\s*#.*\n)*\s*return None\b", limits):
        bad.append("the redirect handler does not return None")
    return bad


def check_logging(root):
    pat = r"\bprint\b|\blogging\b|\blogger\b|\btraceback\b|format_exc|set_debuglevel|\bstr\(e\)|\brepr\(e\)"
    return [f"output vocabulary in {n}: {m.group(0)}" for n, t in two(root).items() for m in grep(pat, t)]


def check_readonly(root):
    pat = (r"open\([^)]*[\"'](?:w|a|x|r\+|wb|ab|xb|w\+)[\"']|\.write\(|os\.(?:remove|rename|unlink|makedirs|mkdir|chmod|"
           r"replace|rmdir|truncate|symlink|link)\b|shutil\.|O_WRONLY|O_RDWR|O_CREAT|O_APPEND|O_TRUNC")
    return [f"a write path in {n}: {m.group(0)}" for n, t in two(root).items() for m in grep(pat, t)]


GROUPS = {"secret": check_secret, "network": check_network, "logging": check_logging, "readonly": check_readonly}


def _plant(root, rel, fn):
    p = os.path.join(root, rel)
    with open(p) as f:
        text = f.read()
    new = fn(text)
    assert new != text, f"plant did not change {rel}"
    with open(p, "w") as f:
        f.write(new)


# (group, file, mutation): each is one violation the group must catch.
PLANTS = [
    ("secret", "corellia_hud/claude_usage.py", lambda t: t + f'\nX = "{CRED_NAME}"\n'),
    ("secret", "corellia_hud/state.py", lambda t: t + '\nY = "refresh_token"\n'),
    ("secret", "static/detail.js", lambda t: t + "\n// Authorization\n"),
    ("secret", "corellia_hud/claude_limits.py", lambda t: t + '\nZ = os.environ.get("HOME")\n'),
    ("network", "corellia_hud/claude_limits.py", lambda t: t + '\nH = "https://evil.example/x"\n'),
    ("network", "corellia_hud/claude_limits.py", lambda t: t + '\nH = "http://plain.example"\n'),
    ("network", "corellia_hud/claude_limits.py", lambda t: t + "\nV = ssl.CERT_NONE\n"),
    ("network", "corellia_hud/claude_limits.py", lambda t: t + '\nR = urllib.request.Request(U, method="PO' + 'ST")\n'),
    ("network", "corellia_hud/claude_usage.py", lambda t: "import socket\n" + t),
    ("network", "corellia_hud/claude_limits.py", lambda t: t.replace("return None   # a 3xx", "return req   # a 3xx")),
    ("logging", "corellia_hud/claude_usage.py", lambda t: t + "\nprint('x')\n"),
    ("logging", "corellia_hud/claude_limits.py", lambda t: t + "\nM = str(e)\n"),
    ("readonly", "corellia_hud/claude_limits.py", lambda t: t + '\nF = open("/tmp/x", "w")\n'),
    ("readonly", "corellia_hud/claude_usage.py", lambda t: t + "\nos.remove('/tmp/x')\n"),
]


def selftest():
    caught = 0
    with tempfile.TemporaryDirectory() as tmp:
        for g, fn in GROUPS.items():
            base = fn(ROOT)
            if base:
                print(f"BASELINE_FAILS group={g}: {base[:3]}")
                return 1
        for i, (group, rel, mutate) in enumerate(PLANTS):
            scratch = os.path.join(tmp, f"s{i}")
            os.makedirs(scratch)
            shutil.copytree(os.path.join(ROOT, "corellia_hud"), os.path.join(scratch, "corellia_hud"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(os.path.join(ROOT, "static"), os.path.join(scratch, "static"))
            _plant(scratch, rel, mutate)
            found = GROUPS[group](scratch)
            if found:
                caught += 1
            else:
                print(f"MISSED group={group} plant={i} file={rel}")
    print(f"SELFTEST_CAUGHT {caught}/{len(PLANTS)}")
    return 0 if caught == len(PLANTS) else 1


def main(argv):
    if argv and argv[0] == "--selftest":
        return selftest()
    group = argv[0] if argv else ""
    if group not in GROUPS:
        print("usage: check_claude_secrets.py <" + "|".join(GROUPS) + "> | --selftest")
        return 2
    problems = GROUPS[group](ROOT)
    for p in problems:
        print("VIOLATION", p)
    if problems:
        return 1
    print(f"CLAUDE_SECRETS_OK group={group}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
