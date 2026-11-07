"""launcher/detail_policy.py is pure (no gi): it decides which URIs may open a detail window."""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("detail_policy", os.path.join(ROOT, "launcher", "detail_policy.py"))
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

OK = "http://127.0.0.1:8670/detail.html?kind=service&id=sieve"


class DetailPolicyTests(unittest.TestCase):
    def test_accepts_the_canonical_detail_uri(self):
        self.assertTrue(policy.allowed(OK))

    def test_accepts_every_kind_with_an_encoded_id(self):
        for kind in ("service", "services", "perf", "site", "chat", "alert"):
            self.assertTrue(policy.allowed(f"http://127.0.0.1:8670/detail.html?kind={kind}&id=a%3Ab"), kind)

    def test_rejects_other_hosts_and_ports(self):
        for uri in ("http://localhost:8670/detail.html?kind=service&id=x", "http://evil.example:8670/detail.html?kind=service&id=x",
                    "http://127.0.0.1:8671/detail.html?kind=service&id=x", "http://127.0.0.1/detail.html?kind=service&id=x",
                    "https://127.0.0.1:8670/detail.html?kind=service&id=x", "http://100.91.184.86:8670/detail.html?kind=service&id=x"):
            self.assertFalse(policy.allowed(uri), uri)

    def test_rejects_other_paths(self):
        for path in ("/", "/index.html", "/api/state", "/detail.html/", "/detail.htm", "/static/detail.html", "//detail.html",
                     "/detail.html/../index.html"):
            self.assertFalse(policy.allowed(f"http://127.0.0.1:8670{path}?kind=service&id=x"), path)

    def test_rejects_non_http_schemes(self):
        for uri in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,<h1>x</h1>", "about:blank", "ftp://127.0.0.1:8670/detail.html?kind=service&id=x"):
            self.assertFalse(policy.allowed(uri), uri)

    def test_rejects_userinfo_and_lookalike_hosts(self):
        for uri in ("http://127.0.0.1:8670@evil.example/detail.html?kind=service&id=x",
                    "http://127.0.0.1:8670.evil.example/detail.html?kind=service&id=x",
                    "http://evil.example@127.0.0.1:8670/detail.html?kind=service&id=x",
                    "http://127.0.0.1:8670\\@evil.example/detail.html?kind=service&id=x"):
            self.assertFalse(policy.allowed(uri), uri)

    def test_rejects_a_missing_or_malformed_query(self):
        for q in ("", "?", "?kind=service", "?id=sieve", "?kind=bogus&id=x", "?kind=service&id=a&id=b",
                  "?kind=service&id=x&extra=1", "?kind=service&id="):
            self.assertFalse(policy.allowed("http://127.0.0.1:8670/detail.html" + q), q)

    def test_rejects_an_over_long_uri(self):
        self.assertFalse(policy.allowed(OK + "x" * 600))

    def test_rejects_non_strings_and_control_characters(self):
        for bad in (None, 5, b"http://127.0.0.1:8670/detail.html?kind=service&id=x", "", OK + "\n", OK + " x", "http://127.0.0.1:8670/detail.html?kind=service&id=a\x00b"):
            self.assertFalse(policy.allowed(bad), repr(bad))

    def test_the_module_is_pure(self):
        text = open(os.path.join(ROOT, "launcher", "detail_policy.py")).read()
        self.assertNotIn("gi", [line.split()[1].split(".")[0] for line in text.splitlines()
                               if line.startswith(("import ", "from "))])


if __name__ == "__main__":
    unittest.main()
