import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from support import ROOT, captured_output, load

compare = load(ROOT / "rpm" / "compare.py")


class FakeCommands:
    """Stands in for SshCommands, recording what would have been run."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.result = (returncode, stdout, stderr)
        self.calls = []

    def run(self, command, timeout):
        self.calls.append((command, timeout))
        return subprocess.CompletedProcess(command, *self.result)


class GatherPackagesTests(unittest.TestCase):
    def gather(self, commands, **kwargs):
        return compare.gather_packages("host", commands=commands, **kwargs)

    def test_parses_packages(self):
        output = (
            "pkg.x86_64\t1.0-1\nepoch.noarch\t0:2.0-3\ngpg-pubkey.(none)\tabc-def\n"
        )
        self.assertEqual(
            self.gather(FakeCommands(output)),
            {"pkg.x86_64": {"1.0-1"}, "epoch.noarch": {"2.0-3"}},
        )

    def test_passes_timeouts(self):
        commands = FakeCommands("a\t1\n")
        self.gather(commands, timeout=99, connect_timeout=7)
        [(command, timeout)] = commands.calls
        self.assertEqual(
            command,
            [
                *("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7", "host"),
                compare.REMOTE_COMMAND,
            ],
        )
        self.assertEqual(timeout, 99)

    def test_errors(self):
        for name, commands, message in (
            ("malformed", FakeCommands("garbage without a tab\n"), "unrecognized"),
            ("empty", FakeCommands(""), "empty rpm output"),
            ("ssh failure", FakeCommands("", 255, "denied\n"), "^denied$"),
            ("silent failure", FakeCommands("", 255), "^ssh exited 255$"),
        ):
            with self.subTest(name), self.assertRaisesRegex(RuntimeError, message):
                self.gather(commands)

    def test_version_key(self):
        versions = ["5.14.0-70.el9", "5.14.0-611.el9", "5.14.0-570.el9"]
        self.assertEqual(
            sorted(versions, key=compare.version_key),
            ["5.14.0-70.el9", "5.14.0-570.el9", "5.14.0-611.el9"],
        )


# Like ssh with a ProxyJump: a helper process keeps the output pipes open.
HANGS_WITH_HELPER = ["sh", "-c", "sleep 30 & exec sleep 30"]
# Under DRAIN_TIMEOUT, so a helper left running (holding the pipes) would fail.
QUICK = compare.DRAIN_TIMEOUT - 1


class SshCommandsTests(unittest.TestCase):
    def test_runs_command(self):
        result = compare.SshCommands().run(
            [sys.executable, "-c", "print('out'); raise SystemExit(3)"], timeout=10
        )
        self.assertEqual((result.returncode, result.stdout), (3, "out\n"))

    def test_stdin_is_not_inherited(self):
        # Give this process a stdin with data in it, which must not leak.
        read_end, write_end = os.pipe()
        os.write(write_end, b"leaked")
        os.close(write_end)
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            result = compare.SshCommands().run(
                [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
                timeout=10,
            )
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(read_end)
        self.assertEqual(result.stdout, "''\n")

    def test_timeout_kills_command_and_helpers(self):
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, r"^timed out after 1s$"):
            compare.SshCommands().run(HANGS_WITH_HELPER, timeout=1)
        self.assertLess(time.monotonic() - start, 1 + QUICK)

    def test_stop_kills_running_commands_and_refuses_new_ones(self):
        commands = compare.SshCommands()
        result = {}

        def worker():
            result["value"] = commands.run(HANGS_WITH_HELPER, timeout=30)

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(100):
            if commands._running:
                break
            time.sleep(0.05)
        commands.stop()
        thread.join(timeout=QUICK)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(result["value"].returncode, 0)  # killed
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            commands.run(HANGS_WITH_HELPER, timeout=30)


class MainTests(unittest.TestCase):
    def run_main(self, hosts, inventories, delays=None):
        self.gather_calls = []

        def gather(host, *args):
            self.gather_calls.append((host, *args))
            time.sleep((delays or {}).get(host, 0))
            result = inventories[host]
            if isinstance(result, BaseException):
                raise result
            return result

        with (
            mock.patch.object(compare, "gather_packages", gather),
            mock.patch.object(sys, "argv", ["compare.py", *hosts]),
            captured_output() as (stdout, stderr),
        ):
            code = compare.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_usage_errors(self):
        for hosts, message in (
            (["one"], "USAGE"),
            (["a", "a"], "duplicate host"),
            (["a", "-oProxyCommand=x"], "cannot start with '-'"),
        ):
            with self.subTest(hosts=hosts):
                code, _, stderr = self.run_main(hosts, {})
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)

    def test_differences_table(self):
        inventories = {
            "h1": {"same.x86_64": {"1"}, "diff.x86_64": {"1"}, "only1.noarch": {"1"}},
            "h2": {"same.x86_64": {"1"}, "diff.x86_64": {"2", "10"}},
        }
        code, stdout, stderr = self.run_main(["h2", "h1"], inventories)
        self.assertEqual(code, 0)
        rows = [line.split() for line in stdout.splitlines()[3:] if line.strip()]
        self.assertEqual(
            rows, [["diff.x86_64", "2,10", "1"], ["only1.noarch", "-", "1"]]
        )
        self.assertIn(
            "Compared 3 package(s) across 2 host(s), 2 difference(s).", stderr
        )

    def test_errors_follow_argument_order(self):
        inventories = {"h1": RuntimeError("one"), "h2": RuntimeError("two")}
        code, _, stderr = self.run_main(["h1", "h2"], inventories, {"h1": 0.3})
        self.assertEqual(code, 1)
        self.assertLess(stderr.index("h1: one"), stderr.index("h2: two"))

    def test_settings_and_shared_commands_reach_every_host(self):
        environ = {"RPM_COMPARE_TIMEOUT": "7", "RPM_COMPARE_CONNECT_TIMEOUT": "3"}
        with mock.patch.dict(os.environ, environ):
            code, _, _ = self.run_main(["h1", "h2"], {"h1": {"a": {"1"}}, "h2": {}})
        self.assertEqual(code, 0)
        hosts, timeouts, connect_timeouts, commands = zip(
            *self.gather_calls, strict=True
        )
        self.assertEqual(sorted(hosts), ["h1", "h2"])
        self.assertEqual(set(timeouts), {7})
        self.assertEqual(set(connect_timeouts), {3})
        self.assertEqual(len(set(map(id, commands))), 1)
        self.assertIsInstance(commands[0], compare.SshCommands)

    def test_interrupt_stops_the_commands_the_hosts_use(self):
        with (
            mock.patch.object(compare.SshCommands, "stop", autospec=True) as stop,
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_main(["h1", "h2"], {"h1": KeyboardInterrupt(), "h2": {}})
        [(stopped,)] = [call.args for call in stop.call_args_list]
        self.assertTrue(all(call[-1] is stopped for call in self.gather_calls))

    def test_host_error_fails(self):
        inventories = {"h1": {"a": {"1"}}, "h2": RuntimeError("unreachable")}
        code, stdout, stderr = self.run_main(["h1", "h2"], inventories)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("ERROR: h2: unreachable", stderr)


if __name__ == "__main__":
    unittest.main()
