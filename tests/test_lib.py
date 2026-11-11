import os
import signal
import subprocess
import textwrap
import unittest
from unittest import mock

from support import python_code, run_in_pty

from lib import env


def run_main(body, setup="", **kwargs):
    """Run lib.runtime.run(lambda: body) in a child interpreter."""
    return subprocess.run(
        python_code(f"{setup}\nfrom lib.runtime import run\nrun(lambda: {body})"),
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        **kwargs,
    )


class RuntimeTests(unittest.TestCase):
    def test_closed_stdout_pipe_exits_quietly(self):
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            result = run_main("print('x' * 1_000_000)", stdout=write_end)
        finally:
            os.close(write_end)
        self.assertEqual((result.returncode, result.stderr), (141, ""))

    def test_interrupt_works_when_parent_ignored_sigint(self):
        result = run_main(
            "os.kill(os.getpid(), signal.SIGINT)",
            setup="import os, signal",
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
        )
        self.assertEqual(result.returncode, 130)
        self.assertIn("Interrupted.", result.stderr)

    def test_terminate_exits_143(self):
        result = run_main("os.kill(os.getpid(), signal.SIGTERM)", "import os, signal")
        self.assertEqual(result.returncode, 143)
        self.assertIn("Interrupted.", result.stderr)

    def test_terminate_is_not_swallowed_by_except_exception(self):
        body = textwrap.dedent("""\
            def main():
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                except Exception:
                    print("swallowed")
            run(main)
            """)
        result = subprocess.run(
            python_code(f"import os, signal\nfrom lib.runtime import run\n{body}"),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((result.returncode, result.stdout), (143, ""))

    def test_unexpected_exception_prints_traceback_and_exits_1(self):
        result = run_main("1 / 0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ZeroDivisionError", result.stderr)

    def test_exit_codes_pass_through(self):
        for body, setup, code in (
            ("None", "", 0),
            ("2", "", 2),
            ("sys.exit(3)", "import sys", 3),
            ("sys.exit()", "import sys", 0),
        ):
            with self.subTest(body=body):
                self.assertEqual(run_main(body, setup).returncode, code)

    def test_exit_with_message_prints_it_and_exits_1(self):
        result = run_main("sys.exit('bad input')", "import sys")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad input", result.stderr)

    def test_non_integer_result_exits_1(self):
        result = run_main("'oops'")
        self.assertEqual(result.returncode, 1)
        self.assertIn("non-integer", result.stderr)


ECHO_CHECK = """\
import termios
from lib.output import hide_interrupt_echo

def echoctl():
    return bool(termios.tcgetattr(0)[3] & termios.ECHOCTL)

attributes = termios.tcgetattr(0)
attributes[3] |= termios.ECHOCTL
termios.tcsetattr(0, termios.TCSANOW, attributes)
"""


MOVED_TO_BACKGROUND_JOB = (
    ECHO_CHECK.split("attributes =")[0]
    + """\
import os, time

def foreground():
    return os.tcgetpgrp(0) == os.getpgrp()

while not foreground():
    time.sleep(0.01)
with hide_interrupt_echo():
    print("inside", echoctl(), flush=True)
    while foreground():
        time.sleep(0.01)
print("after", echoctl(), flush=True)
"""
)


def job_control_leader(job_code):
    """Code for a session leader that runs job_code like a shell job."""
    return ECHO_CHECK + textwrap.dedent(f"""\
        import os, signal, subprocess
        job = subprocess.Popen(
            {python_code(job_code)!r}, process_group=0, stdout=subprocess.PIPE, text=True
        )
        os.tcsetpgrp(0, job.pid)
        print(job.stdout.readline().strip())
        # Ignored only now: the job would inherit an ignored SIGTTOU.
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(0, os.getpgrp())
        _, status = os.waitpid(job.pid, os.WUNTRACED)
        if os.WIFSTOPPED(status):
            print("job stopped")
            job.kill()
        else:
            print(job.stdout.read().strip())
        """)


class HideInterruptEchoTests(unittest.TestCase):
    def test_foreground_job_hides_and_restores_echo(self):
        code = ECHO_CHECK + textwrap.dedent("""\
            with hide_interrupt_echo():
                print("inside", echoctl())
            print("after", echoctl())
            """)
        returncode, output = run_in_pty(python_code(code), controlling=True)
        self.assertEqual(returncode, 0, output)
        self.assertIn("inside False\nafter True", output)

    def test_settings_changed_meanwhile_are_not_undone(self):
        # Like a pager reading this output, which puts the terminal in raw mode
        # after the tool started; restoring blindly would undo that.
        code = ECHO_CHECK + textwrap.dedent("""\
            def icanon():
                return bool(termios.tcgetattr(0)[3] & termios.ICANON)

            with hide_interrupt_echo():
                pager = termios.tcgetattr(0)
                pager[3] &= ~termios.ICANON
                termios.tcsetattr(0, termios.TCSANOW, pager)
            print("after", echoctl(), icanon())
            """)
        returncode, output = run_in_pty(python_code(code), controlling=True)
        self.assertEqual(returncode, 0, output)
        self.assertIn("after False False", output)

    def test_background_job_leaves_terminal_alone(self):
        # Hand the terminal to another process group, so this one is a
        # background job when it starts.
        code = ECHO_CHECK + textwrap.dedent("""\
            import os, subprocess
            other = subprocess.Popen(["sleep", "30"], process_group=0)
            os.tcsetpgrp(0, other.pid)
            try:
                with hide_interrupt_echo():
                    print("inside", echoctl())
            finally:
                other.kill()
            """)
        returncode, output = run_in_pty(python_code(code), controlling=True)
        self.assertEqual(returncode, 0, output)
        self.assertIn("inside True", output)

    def test_job_moved_to_background_is_not_stopped_on_restore(self):
        # Like a shell: the session leader runs the job in its own process
        # group in the foreground, then moves it to the background (Ctrl-Z,
        # bg). Restoring the settings from the background would then stop
        # the job with SIGTTOU, unless it is ignored.
        returncode, output = run_in_pty(
            python_code(job_control_leader(MOVED_TO_BACKGROUND_JOB)), controlling=True
        )
        self.assertEqual(returncode, 0, output)
        self.assertIn("inside False\nafter True", output)
        self.assertNotIn("stopped", output)


class EnvTests(unittest.TestCase):
    def test_positive_int(self):
        cases = {None: 8, "": 8, "4": 4, " 4 ": 4, "0": 8, "-2": 8, "abc": 8}
        for value, expected in cases.items():
            environ = {} if value is None else {"TEST_VALUE": value}
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, environ, clear=True),
                mock.patch.object(env, "status") as status,
            ):
                self.assertEqual(env.positive_int("TEST_VALUE", 8), expected)
                self.assertEqual(status.called, value in {"0", "-2", "abc"})


if __name__ == "__main__":
    unittest.main()
