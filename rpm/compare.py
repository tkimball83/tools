#!/usr/bin/env python3

import os
import re
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path

from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.env import positive_int
from lib.output import spinner, status
from lib.runtime import run

IGNORED_PACKAGES = {"gpg-pubkey"}
CONNECT_TIMEOUT = 10
MAX_WORKERS = 8
SSH_TIMEOUT = 30
# How long to wait for output pipes to close after killing a command.
DRAIN_TIMEOUT = 5
REMOTE_COMMAND = (
    r"rpm -qa --queryformat "
    r"'%{NAME}.%{ARCH}\t%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\n'"
)


class SshCommands:
    """Runs commands, and can kill those still running when the tool exits.

    Timeouts are enforced by the worker threads, which are abandoned on exit,
    so without this an ssh to a hung host would outlive the tool. Each command
    gets its own process group, so killing it also kills helpers such as a
    ProxyJump or ProxyCommand ssh, which would otherwise hold its pipes open.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._running: set[subprocess.Popen] = set()
        self._stopped = False

    def run(self, command: list[str], timeout: int) -> subprocess.CompletedProcess:
        with self._lock:
            if self._stopped:
                raise RuntimeError("cancelled")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                process_group=0,
            )
            self._running.add(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(process)
            # Give up on output still held open by something outside the group.
            with suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=DRAIN_TIMEOUT)
            raise RuntimeError(f"timed out after {timeout}s") from None
        finally:
            with self._lock:
                self._running.discard(process)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def stop(self):
        with self._lock:
            self._stopped = True
            for process in self._running:
                self._kill(process)

    @staticmethod
    def _kill(process: subprocess.Popen):
        # An unreaped process keeps its pid, so the group id cannot be reused.
        if process.returncode is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)


def gather_packages(
    host: str,
    timeout: int = SSH_TIMEOUT,
    connect_timeout: int = CONNECT_TIMEOUT,
    commands: SshCommands | None = None,
) -> dict[str, set[str]]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        host,
    ]
    result = (commands or SshCommands()).run([*command, REMOTE_COMMAND], timeout)
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise RuntimeError(detail or f"ssh exited {result.returncode}")
    output = result.stdout
    packages: dict[str, set[str]] = {}
    malformed = 0
    for line in output.splitlines():
        name, _, version = line.partition("\t")
        if not name or not version:
            malformed += bool(line.strip())
            continue
        if name.rsplit(".", 1)[0] in IGNORED_PACKAGES:
            continue
        packages.setdefault(name, set()).add(version.removeprefix("0:"))
    if malformed:
        raise RuntimeError(f"{malformed} unrecognized line(s) in rpm output")
    if not packages:
        raise RuntimeError("empty rpm output")
    return packages


def version_key(version: str) -> list[str | int]:
    return [
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", version)
    ]


def usage() -> int:
    status(f"USAGE: {Path(sys.argv[0]).name} HOST HOST [HOST ...]")
    return 2


def main() -> int:
    if len(sys.argv) < 3:
        return usage()
    hosts = sys.argv[1:]
    if len(set(hosts)) != len(hosts):
        status("ERROR: duplicate host")
        return usage()
    if any(host.startswith("-") for host in hosts):
        status("ERROR: hosts cannot start with '-'")
        return usage()
    timeout = positive_int("RPM_COMPARE_TIMEOUT", SSH_TIMEOUT)
    connect_timeout = positive_int("RPM_COMPARE_CONNECT_TIMEOUT", CONNECT_TIMEOUT)

    inventories = {}
    errors = {}
    commands = SshCommands()
    pool = ThreadPoolExecutor(max_workers=min(len(hosts), MAX_WORKERS))
    try:
        with spinner(f"Gathering packages from {len(hosts)} host(s)"):
            futures = {
                pool.submit(gather_packages, h, timeout, connect_timeout, commands): h
                for h in hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    inventories[host] = future.result()
                except (OSError, RuntimeError) as exc:
                    errors[host] = exc
    finally:
        # On Ctrl-C or SIGTERM, skip queued hosts and kill running ssh calls
        # without waiting; run() then exits with os._exit, which does not join
        # the worker threads.
        commands.stop()
        pool.shutdown(wait=False, cancel_futures=True)
    for host in hosts:
        if host in errors:
            status(f"ERROR: {host}: {errors[host]}")
        else:
            status(f"Found {len(inventories[host])} package(s) on {host}")
    if errors:
        return 1

    names = sorted(set().union(*inventories.values()))
    rows = []
    for name in names:
        versions = [
            ",".join(sorted(inventories[host].get(name, ()), key=version_key)) or "-"
            for host in hosts
        ]
        if len(set(versions)) > 1:
            rows.append((name, *versions))

    if rows:
        table = tabulate(rows, headers=["Package", *hosts], tablefmt="simple")
        print(f"\n{table}\n", flush=True)

    status(
        f"Compared {len(names)} package(s) across {len(hosts)} host(s), {len(rows)} difference(s)."
    )
    return 0


if __name__ == "__main__":
    run(main)
