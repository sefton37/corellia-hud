"""journal.tail: one hardened path to journalctl. The runner is injected, so no real journal is read."""
import os
import subprocess
import unittest

from corellia_hud import journal
from tests.support.fake_journalctl import SECRET

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support", "fake_journalctl.py")


class Result:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class Recorder:
    """A stand-in for the process runner: records every call and returns scripted results."""

    def __init__(self, *results, raises=None):
        self.calls, self.results, self.raises = [], list(results), raises

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.raises:
            raise self.raises
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def lines(n, secret_last=False):
    out = [f"2026-11-05T12:00:{i:02d}-06:00 host u[1]: line {i}" for i in range(n)]
    if secret_last and out:
        out[-1] += f" token={SECRET}"
    return ("\n".join(out) + "\n").encode()


class ArgvTests(unittest.TestCase):
    def test_system_unit_argv_is_the_exact_fixed_template(self):
        run = Recorder(Result(lines(3)))
        journal.tail("portfolio-chat.service", ["system"], runner=run)
        self.assertEqual(run.calls[0][0], ["journalctl", "--no-pager", "--output=short-iso", "--lines=20",
                                           "--unit=portfolio-chat.service"])

    def test_user_unit_argv_adds_user_flag(self):
        run = Recorder(Result(lines(3)))
        journal.tail("flint.service", ["user"], runner=run)
        self.assertEqual(run.calls[0][0], ["journalctl", "--user", "--no-pager", "--output=short-iso",
                                           "--lines=20", "--unit=flint.service"])

    def test_shell_is_never_true_and_a_timeout_is_set(self):
        run = Recorder(Result(lines(3)))
        journal.tail("sieve.service", ["system"], runner=run)
        kw = run.calls[0][1]
        self.assertIs(kw.get("shell"), False)
        self.assertLessEqual(kw["timeout"], 3)
        self.assertGreater(kw["timeout"], 0)

    def test_binary_is_configurable_but_the_rest_of_argv_is_not(self):
        run = Recorder(Result(lines(1)))
        journal.tail("a.service", ["system"], runner=run, binary="/opt/fake")
        self.assertEqual(run.calls[0][0][0], "/opt/fake")
        self.assertEqual(run.calls[0][0][1:3], ["--no-pager", "--output=short-iso"])


class UnitValidationTests(unittest.TestCase):
    HOSTILE = ["x.service --vacuum-size=1M", "a;touch X", "$(touch X)", "-x.service", "a" * 300 + ".service",
               "x.service\nz.service", "x.socket", "", "a b.service", "../x.service", "`id`.service"]

    def test_hostile_unit_names_are_refused_and_the_runner_is_never_called(self):
        for name in self.HOSTILE:
            run = Recorder(Result(lines(3)))
            res = journal.tail(name, ["system"], runner=run)
            self.assertEqual(res["state"], "unit_invalid", repr(name))
            self.assertEqual(res["lines"], [])
            self.assertEqual(run.calls, [], repr(name))

    def test_valid_unit_names_pass(self):
        for name in ("sieve.service", "corellia-backup.timer", "ollama@gpu0.service", "a_b.c-d.service"):
            run = Recorder(Result(lines(2)))
            self.assertEqual(journal.tail(name, ["system"], runner=run)["state"], "ok", name)


class StateTests(unittest.TestCase):
    def test_timeout_state(self):
        run = Recorder(Result(), raises=subprocess.TimeoutExpired("journalctl", 3))
        self.assertEqual(journal.tail("a.service", ["system"], runner=run)["state"], "timeout")

    def test_permission_denied_state(self):
        run = Recorder(Result(b"", b"No journal files were opened due to insufficient permissions.\n", 1))
        self.assertEqual(journal.tail("a.service", ["system"], runner=run)["state"], "no_permission")

    def test_empty_output_is_no_lines_never_an_empty_ok(self):
        for out in (b"", b"-- No entries --\n", b"-- Journal begins at X --\n"):
            run = Recorder(Result(out))
            res = journal.tail("a.service", ["system"], runner=run)
            self.assertEqual(res["state"], "no_lines", out)
            self.assertEqual(res["lines"], [])

    def test_missing_binary_is_unavailable(self):
        run = Recorder(Result(), raises=FileNotFoundError("journalctl"))
        self.assertEqual(journal.tail("a.service", ["system"], runner=run)["state"], "unavailable")

    def test_other_failures_never_raise(self):
        run = Recorder(Result(), raises=OSError("boom"))
        self.assertEqual(journal.tail("a.service", ["system"], runner=run)["state"], "unavailable")


class OutputTests(unittest.TestCase):
    def test_at_most_20_lines_keeping_the_tail(self):
        run = Recorder(Result(lines(30)))
        res = journal.tail("a.service", ["system"], runner=run)
        self.assertEqual(len(res["lines"]), 20)
        self.assertIn("line 29", res["lines"][-1])
        self.assertIn("line 10", res["lines"][0])

    def test_each_line_is_capped_at_400_chars(self):
        run = Recorder(Result(("x " * 600 + "\n").encode()))
        res = journal.tail("a.service", ["system"], runner=run)
        self.assertTrue(all(len(line) <= 400 for line in res["lines"]))

    def test_journal_lines_are_returned_raw(self):
        raw = lines(5, secret_last=True) + f"Authorization: Bearer {SECRET}\n".encode()
        self.assertIn(SECRET.encode(), raw)                       # positive control: in the fake output
        run = Recorder(Result(raw))
        res = journal.tail("a.service", ["system"], runner=run)
        self.assertIn(f"Authorization: Bearer {SECRET}", res["lines"][-1])   # verbatim
        self.assertIn(f"token={SECRET}", res["lines"][-2])                   # verbatim

    def test_non_utf8_output_does_not_raise(self):
        run = Recorder(Result(b"ok \xff\xfe line\n"))
        self.assertEqual(journal.tail("a.service", ["system"], runner=run)["state"], "ok")

    def test_real_subprocess_runner_against_the_fake_journalctl(self):
        res = journal.tail("sieve.service", ["system"], binary=FAKE)
        self.assertEqual(res["state"], "ok")
        self.assertEqual(len(res["lines"]), 20)
        self.assertIn(SECRET, "\n".join(res["lines"]))


class ScopeTests(unittest.TestCase):
    HOME = os.path.expanduser("~")

    def svc(self, kind, unit="x.service", unit_path=None):
        return {"name": "x", "kind": kind, "unit": unit, "unit_path": unit_path}

    def test_scope_resolution_table(self):
        cases = [
            (self.svc("systemd"), ["system"]),
            (self.svc("systemd-user"), ["user"]),
            (self.svc("python", unit_path="/etc/systemd/system/x.service"), ["system"]),
            (self.svc("docker", unit_path="/etc/systemd/system/x.service"), ["system"]),
            (self.svc("python", unit_path="~/.config/systemd/user/x.service"), ["user"]),
            (self.svc("docker", unit_path=self.HOME + "/.config/systemd/user/x.service"), ["user"]),
            (self.svc("python"), ["system", "user"]),
            (self.svc("docker", unit_path="/somewhere/else.service"), ["system", "user"]),
        ]
        for svc, want in cases:
            self.assertEqual(journal.scopes_for(svc), want, svc)

    def test_timer_maps_to_its_service(self):
        self.assertEqual(journal.journal_unit("corellia-backup.timer"), "corellia-backup.service")
        self.assertEqual(journal.journal_unit("sieve.service"), "sieve.service")

    def test_no_unit_means_no_journal(self):
        self.assertIsNone(journal.journal_unit(None))
        self.assertIsNone(journal.journal_unit(""))

    def test_second_scope_is_tried_when_the_first_has_no_lines(self):
        run = Recorder(Result(b"-- No entries --\n"), Result(lines(2)))
        res = journal.tail("x.service", ["system", "user"], runner=run)
        self.assertEqual(res["state"], "ok")
        self.assertEqual(len(run.calls), 2)
        self.assertIn("--user", run.calls[1][0])


if __name__ == "__main__":
    unittest.main()
