import os
import shutil
import signal
import sys
import termios
from contextlib import contextmanager, suppress

from yaspin import Spinner, yaspin

FRAMES = Spinner(frames=["[\\]:", "[|]:", "[/]:", "[-]:"], interval=100)
SPINNER_MARGIN = 8


def status(message):
    for line in str(message).splitlines() or [""]:
        print(f"[*]: {line}", file=sys.stderr)


@contextmanager
def spinner(text):
    if not sys.stderr.isatty() or shutil.get_terminal_size().columns < SPINNER_MARGIN:
        status(text)
        yield
        return
    with yaspin(FRAMES, text=text, stream=sys.stderr) as sp:
        yield
        try:
            sp.ok("[*]:")
        except ValueError:
            status(text)


def _set_terminal(fd, attributes):
    # A job moved to the background mid-run (Ctrl-Z, bg) is stopped by SIGTTOU
    # when it changes terminal settings, unless SIGTTOU is ignored.
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        with suppress(OSError, termios.error):
            termios.tcsetattr(fd, termios.TCSANOW, attributes)
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _get_terminal(fd):
    try:
        return termios.tcgetattr(fd)
    except OSError, termios.error:
        return None


@contextmanager
def hide_interrupt_echo():
    """Stop the terminal echoing ^C while the body runs (main thread only)."""
    try:
        fd = sys.stdin.fileno()
        # Leave the terminal alone unless this is the foreground job.
        foreground = os.tcgetpgrp(fd) == os.getpgrp()
        old = termios.tcgetattr(fd) if foreground else None
    except AttributeError, ValueError, OSError, termios.error:
        old = None
    if old is None:
        yield
        return
    new = old.copy()
    new[3] &= ~termios.ECHOCTL
    applied = None
    try:
        _set_terminal(fd, new)
        applied = _get_terminal(fd)
        yield
    finally:
        # Restore only if nothing else changed the terminal meanwhile, such as
        # a pager reading this output, whose settings must not be undone.
        if applied is not None and _get_terminal(fd) == applied:
            _set_terminal(fd, old)
