#!/usr/bin/env python3

import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.output import spinner, status
from lib.runtime import run

IGNORED_PACKAGES = {"gpg-pubkey"}
SSH_TIMEOUT = 30
REMOTE_COMMAND = (
    r"rpm -qa --queryformat "
    r"'%{NAME}.%{ARCH}\t%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\n'"
)


def gather_packages(host: str) -> dict[str, set[str]]:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host]
    try:
        result = subprocess.run(
            [*command, REMOTE_COMMAND],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SSH_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {SSH_TIMEOUT}s") from None
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
        return usage()

    inventories = {}
    errors = {}
    with (
        spinner(f"Gathering packages from {len(hosts)} host(s)"),
        ThreadPoolExecutor(max_workers=min(len(hosts), 8)) as pool,
    ):
        futures = {pool.submit(gather_packages, h): h for h in hosts}
        for future in as_completed(futures):
            host = futures[future]
            try:
                inventories[host] = future.result()
            except (OSError, RuntimeError) as exc:
                errors[host] = exc
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
