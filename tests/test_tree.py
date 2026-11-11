import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from support import ROOT, run_in_pty

TREE = ROOT / "rclone" / "tree.sh"
SHYAML = Path(sys.executable).parent / "shyaml"

# Logs its arguments and lists the config's remotes like rclone; can fail,
# reject options or hang on request.
FAKE_RCLONE = """\
#!/bin/sh
printf '%s\\n' "$*" >>"${FAKE_RCLONE_LOG}"
case " $* " in
  *" listremotes "*)
    for argument in "$@"; do
      case "${argument}" in
        --config=*) sed -n 's/^\\[\\(.*\\)\\]$/\\1:/p' "${argument#--config=}" ;;
      esac
    done
    ;;
  *" --bwlimit=bogus "*" version "*)
    echo 'Error: invalid argument "bogus" for "--bwlimit" flag' >&2
    exit 2
    ;;
  *" sync "*)
    env | grep '^RCLONE_' | sed 's/^/env /' >>"${FAKE_RCLONE_LOG}"
    if [ -n "${FAKE_RCLONE_READ_STDIN}" ]; then
      IFS= read -r line
      echo "stdin ${line}" >>"${FAKE_RCLONE_LOG}"
    fi
    [ -z "${FAKE_RCLONE_SYNC_FAIL}" ] || exit 1
    if [ -n "${FAKE_RCLONE_SLEEP}" ]; then
      echo "$$" >"${FAKE_RCLONE_PID}"
      exec sleep "${FAKE_RCLONE_SLEEP}"
    fi
    ;;
esac
"""

SAMPLE_TREE = """\
tree:
  - trunk: primary
    branches:
      - name: backup-a
      - name: backup-b
        leafs:
          - Documents
          - Pictures
"""

REMOTES = [
    "primary",
    "backup-a",
    "backup-b",
    "src",
    "dst",
    "ok",
    "typo",
    "notalist",
    "types",
    "empty",
    "a",
    "b",
    "c",
    "d",
]


@unittest.skipUnless(SHYAML.exists(), "shyaml is not installed in the venv")
class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.rclone = self.tmp / "rclone"
        self.rclone.write_text(FAKE_RCLONE)
        self.rclone.chmod(self.rclone.stat().st_mode | stat.S_IXUSR)
        self.config = self.tmp / "rclone.conf"
        self.config.write_text(
            "".join(f"[{remote}]\ntype = local\n\n" for remote in REMOTES)
        )
        self.log = self.tmp / "log"

    def command(self, tree_yaml, args=(), shyaml=SHYAML):
        tree = self.tmp / "tree.yaml"
        tree.write_text(textwrap.dedent(tree_yaml))
        return [
            str(TREE),
            "-b", str(self.rclone),
            "-c", str(self.config),
            "-f", str(tree),
            "-s", str(shyaml),
            *args,
        ]  # fmt: skip

    def env(self, extra=None):
        return {**os.environ, "FAKE_RCLONE_LOG": str(self.log), **(extra or {})}

    def sync_lines(self):
        log = self.log.read_text().splitlines() if self.log.exists() else []
        return [line for line in log if line.startswith("sync ")]

    def syncs(self):
        return [line.split(" ")[-2:] for line in self.sync_lines()]

    def run_tree(self, tree_yaml, extra_env=None, args=(), stdin="", shyaml=SHYAML):
        return subprocess.run(
            self.command(tree_yaml, args, shyaml),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=self.env(extra_env),
        )

    def assert_fails_without_syncing(self, result, *messages):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.syncs(), [])
        for message in messages:
            self.assertIn(message, result.stderr)

    def test_sample_config(self):
        result = self.run_tree(SAMPLE_TREE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.syncs(),
            [
                ["primary:", "backup-a:"],
                ["primary:Documents", "backup-b:Documents"],
                ["primary:Pictures", "backup-b:Pictures"],
            ],
        )
        self.assertIn("Completed 3 rclone sync command(s).", result.stdout)
        self.assertIn("--disable-http2", self.sync_lines()[0].split(" "))

    def test_rclone_keeps_stdin_for_password_prompts(self):
        # Background commands get /dev/null as stdin unless it is passed on,
        # and rclone reads an encrypted config's password from stdin.
        result = self.run_tree(
            "tree: [{trunk: a, branches: [{name: b}]}]\n",
            {"FAKE_RCLONE_READ_STDIN": "1"},
            stdin="secret\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdin secret", self.log.read_text().splitlines())

    def test_rclone_environment_settings_are_not_passed_on(self):
        # Settings go to rclone as flags; inherited RCLONE_* variables with the
        # same names would otherwise override or conflict with them.
        names = [
            "BWLIMIT",
            "CHECKERS",
            "CONFIG",
            "DRY_RUN",
            "PROGRESS",
            "TRANSFERS",
            "VERBOSE",
        ]
        extra = {f"RCLONE_{name}": "x" for name in names}
        extra["RCLONE_KEEP"] = "kept"  # anything else is left alone
        result = self.run_tree(SAMPLE_TREE, extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text().splitlines()
        self.assertIn("env RCLONE_KEEP=kept", log)
        self.assertEqual(
            [line for line in log if line.startswith("env ") and "KEEP" not in line],
            [],
        )

    def test_flags_and_trailing_colons(self):
        result = self.run_tree(
            """\
            tree:
              - trunk: "src:"
                branches:
                  - name: "dst:"
                    leafs: ["Documents/"]
            """,
            args=["-d", "-vv", "-2", "-k", "4", "-t", "2", "-l", "1M"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.syncs(), [["src:Documents", "dst:Documents"]])
        arguments = self.sync_lines()[0].split(" ")
        for flag in (
            "--dry-run",
            "-vv",
            "--checkers=4",
            "--transfers=2",
            "--bwlimit=1M",
        ):
            self.assertIn(flag, arguments)
        self.assertNotIn("--disable-http2", arguments)
        self.assertNotIn("--progress", arguments)  # stdout is not a terminal

    def test_progress_only_on_a_terminal_without_q(self):
        for args, expected in (((), True), (("-q",), False)):
            with self.subTest(args=args):
                self.log.unlink(missing_ok=True)
                returncode, output = run_in_pty(
                    self.command(SAMPLE_TREE, args), env=self.env()
                )
                self.assertEqual(returncode, 0, output)
                self.assertEqual("--progress" in self.sync_lines()[0], expected)

    def test_command_line_errors(self):
        for args, message in (
            (["-k", "0"], "checkers value must be a positive integer"),
            (["-t", "x"], "transfers value must be a positive integer"),
            (["extra"], "Unexpected argument(s): extra"),
            (["-z"], "Invalid option: -z"),
            (["-f"], "Option -f requires an argument"),
        ):
            with self.subTest(args=args):
                self.assert_fails_without_syncing(
                    self.run_tree(SAMPLE_TREE, args=args), message
                )

    def test_help(self):
        result = self.run_tree(SAMPLE_TREE, args=["-h"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)
        self.assertEqual(self.syncs(), [])

    def test_config_problems_stop_before_any_sync(self):
        result = self.run_tree("""\
            tree:
              - trunk: src
                branchs: []
                branches:
                  - name: ok
                  - name: src
                  - name: typo
                    leaves: [Documents]
                  - name: notalist
                    leafs: Documents
                  - name: types
                    leafs: [~, 1.10, yes, "1.10"]
                  - name:
                  - name: ":"
                  - name: "a:b"
                  - name: nosuch
                  - name: /Volumes/Backup
              - trunk: empty
                branches: []
            """)
        self.assert_fails_without_syncing(
            result,
            "tree.0 has unknown key(s): branchs",
            "tree.0.branches.1 syncs src to itself",
            "tree.0.branches.2 has unknown key(s): leaves",
            "tree.0.branches.3.leafs for src -> notalist must be a non-empty list",
            "tree.0.branches.4.leafs.0 must be a non-empty string",
            "tree.0.branches.4.leafs.1 must be a non-empty string",
            "tree.0.branches.4.leafs.2 must be a non-empty string",
            "tree.0.branches.5.name must be a remote from",
            "tree.0.branches.6.name must be a remote from",
            "tree.0.branches.7.name must be a remote from",
            "tree.0.branches.8.name must be a remote from",
            "tree.0.branches.9.name must be a remote from",
            "tree.1.branches for empty must be a non-empty list",
            "Found 13 problem(s)",
        )
        self.assertNotIn("leafs.3", result.stderr)  # quoted "1.10" is fine

    def test_overlapping_and_chained_syncs_are_rejected(self):
        whole = "- trunk: a\n  branches: [{name: c}]\n"
        part = "- trunk: b\n  branches: [{name: c, leafs: [Documents]}]\n"
        chained = "- trunk: c\n  branches: [{name: d}]\n"
        for order, message in (
            (whole + part, "a: -> c: and b:Documents -> c:Documents"),
            (part + whole, "b:Documents -> c:Documents and a: -> c:"),
        ):
            with self.subTest(message=message):
                self.log.unlink(missing_ok=True)
                result = self.run_tree(
                    "tree:\n" + textwrap.indent(order + chained, "  ")
                )
                self.assert_fails_without_syncing(
                    result,
                    f"{message} write to overlapping paths",
                    "c is both synced to and synced from",
                )

    def test_multiline_names_and_leafs_are_rejected(self):
        result = self.run_tree("""\
            tree:
              - trunk: a
                branches:
                  - name: "b\\nc"
                  - name: c
                    leafs: ["x\\ny"]
            """)
        self.assert_fails_without_syncing(
            result,
            "tree.0.branches.0.name must be a remote from",
            "tree.0.branches.1.leafs.0 must be a non-empty string",
        )

    def test_nested_leafs_overlap(self):
        for leafs, message in (
            (
                "[Docs, Docs/Work, Docsy]",
                "a:Docs -> b:Docs and a:Docs/Work -> b:Docs/Work",
            ),
            (
                "[Docs/Work, Docs, Docsy]",
                "a:Docs/Work -> b:Docs/Work and a:Docs -> b:Docs",
            ),
        ):
            with self.subTest(leafs=leafs):
                self.log.unlink(missing_ok=True)
                result = self.run_tree(
                    f"tree: [{{trunk: a, branches: [{{name: b, leafs: {leafs}}}]}}]\n"
                )
                self.assert_fails_without_syncing(
                    result,
                    f"{message} write to overlapping paths",
                    "Found 1 problem(s)",
                )

    def test_chained_remote_is_reported_once(self):
        result = self.run_tree("""\
            tree:
              - trunk: a
                branches: [{name: c, leafs: [Docs, Pics, Music]}]
              - trunk: c
                branches: [{name: b}]
            """)
        self.assert_fails_without_syncing(result, "Found 1 problem(s)")
        self.assertEqual(result.stderr.count("c is both synced to and synced from"), 1)

    def test_ambiguous_leafs_are_rejected(self):
        result = self.run_tree("""\
            tree:
              - trunk: a
                branches:
                  - name: b
                    leafs: [".", "./Docs", "/Docs", "x//y", "..", "x/../y", "x/./y"]
                  - name: c
                    leafs: [ok, "ok/sub/", ".hidden", "x..y"]
            """)
        self.assert_fails_without_syncing(
            result,
            *(f"tree.0.branches.0.leafs.{i} must be a relative path" for i in range(7)),
            "Found 8 problem(s)",  # the 8th: c:ok and c:ok/sub overlap
        )
        self.assertNotIn("branches.1.leafs", result.stderr)

    def test_failed_lookups_never_widen_a_sync(self):
        # If shyaml fails, a key must not look missing: a missing leafs key
        # means syncing the whole remote.
        flaky = self.tmp / "shyaml"
        flaky.write_text(
            '#!/bin/sh\n[ "$*" = "${FLAKY_SHYAML_FAIL}" ] && exit 1\n'
            f'exec "{SHYAML}" "$@"\n'
        )
        flaky.chmod(0o755)
        tree = "tree: [{trunk: a, branches: [{name: b, leafs: [Docs]}]}]\n"
        for lookup in (
            "keys-0 tree.0",
            "get-type tree.0.branches.0",
            "keys-0 tree.0.branches.0",
            "get-type tree.0.branches.0.leafs",
            "get-length tree.0.branches.0.leafs",
        ):
            with self.subTest(lookup=lookup):
                self.log.unlink(missing_ok=True)
                result = self.run_tree(
                    tree, {"FLAKY_SHYAML_FAIL": lookup}, shyaml=flaky
                )
                self.assert_fails_without_syncing(result, "nothing was synced")

    def test_missing_tree_key(self):
        self.assert_fails_without_syncing(
            self.run_tree("trees: []\n"), "Unable to parse", "missing key 'tree'"
        )

    def test_duplicate_syncs_run_once(self):
        result = self.run_tree("""\
            tree:
              - trunk: src
                branches:
                  - name: dst
                  - name: dst
            """)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.syncs(), [["src:", "dst:"]])
        self.assertIn("Skipping duplicate sync src: -> dst:", result.stderr)

    def test_signals_stop_the_running_sync(self):
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.subTest(signal=sig.name):
                self.log.unlink(missing_ok=True)
                pid_file = self.tmp / "rclone.pid"
                pid_file.unlink(missing_ok=True)
                process = subprocess.Popen(
                    self.command(SAMPLE_TREE),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    env=self.env(
                        {"FAKE_RCLONE_SLEEP": "30", "FAKE_RCLONE_PID": str(pid_file)}
                    ),
                )
                try:
                    for _ in range(200):
                        if pid_file.exists() and pid_file.read_text().strip():
                            break
                        time.sleep(0.05)
                    else:
                        self.fail("rclone never started")
                    # Like Ctrl-C or kill at a terminal: the whole job for
                    # SIGINT, only the script itself for SIGTERM.
                    if sig == signal.SIGINT:
                        os.killpg(process.pid, sig)
                    else:
                        process.send_signal(sig)
                    _, stderr = process.communicate(timeout=10)
                finally:
                    process.kill()
                self.assertEqual(process.returncode, code, stderr)
                self.assertIn("Interrupted.", stderr)
                self.assertEqual(len(self.sync_lines()), 1)  # nothing further
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)

    def test_sync_failures_are_counted(self):
        result = self.run_tree(SAMPLE_TREE, {"FAKE_RCLONE_SYNC_FAIL": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("3 of 3 rclone sync command(s) failed", result.stderr)

    def test_rejected_options_fail_before_syncing(self):
        # The fake, like rclone, only rejects the bad value if it is passed on.
        result = self.run_tree(SAMPLE_TREE, args=["-l", "bogus"])
        self.assert_fails_without_syncing(
            result, 'rclone rejected the options: Error: invalid argument "bogus"'
        )

    def test_invalid_yaml_reports_parser_error(self):
        result = self.run_tree("tree: [a\n")
        self.assert_fails_without_syncing(result)
        self.assertRegex(result.stderr, r"Unable to parse .*ParserError")


if __name__ == "__main__":
    unittest.main()
