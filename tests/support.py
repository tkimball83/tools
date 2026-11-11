"""Shared test helpers. Run the tests with: make check"""

import contextlib
import fcntl
import importlib.util
import io
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load(path: Path):
    """Import a script by path, as its file stem."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def python_code(code: str) -> list[str]:
    """argv running code in a child interpreter that can import lib."""
    return [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{code}",
    ]


@contextlib.contextmanager
def captured_output():
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        yield stdout, stderr


def _take_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def run_in_pty(argv, env=None, controlling=False, timeout=20):
    """Run argv with a 120-column pty as stdin/stdout/stderr.

    With controlling=True the pty is also the child's controlling terminal
    and the child is its foreground job, like a command typed in a shell.
    Returns (returncode, output); raises TimeoutError if the child is still
    running, or the pty still open, after timeout seconds.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    process = subprocess.Popen(
        argv,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=not controlling,
        # Tests run one at a time, so the threads caveat does not apply.
        preexec_fn=_take_terminal if controlling else None,  # noqa: PLW1509
    )
    os.close(slave)
    output = b""
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                raise TimeoutError(f"{argv[0]} still running after {timeout}s")
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO once the child side is closed (Linux)
                break
            if not chunk:
                break
            output += chunk
        try:
            process.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:  # the pty closed, the child lives on
            raise TimeoutError(f"{argv[0]} still running after {timeout}s") from None
    finally:
        os.close(master)
        process.kill()
    return process.returncode, output.decode(errors="replace").replace("\r\n", "\n")
