#!/usr/bin/env python3

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pymediainfo import MediaInfo
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.env import positive_int
from lib.output import spinner, status
from lib.runtime import run

ENGLISH = {"en", "eng"}
DEFAULT_WORKERS = 8
WORKERS_VARIABLE = "CHECK_LANGUAGE_WORKERS"
# An explicit list, because mimetypes reads the OS's mime.types files and
# their mappings differ (Debian maps .ts to Qt Linguist translations).
VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".dv", ".f4v", ".flv", ".m1v",
    ".m2ts", ".m2v", ".m4v", ".mkv", ".mov", ".mp4", ".mpe", ".mpeg", ".mpg",
    ".mts", ".ogm", ".ogv", ".qt", ".rm", ".rmvb", ".ts", ".vob", ".webm",
    ".wmv", ".wtv",
}  # fmt: skip
# These are also TypeScript source extensions, so their contents are checked.
TRANSPORT_STREAM_EXTENSIONS = {".mts", ".ts"}
# MPEG transport stream packets start with a sync byte: 188-byte packets at
# offset 0, or 192-byte Blu-ray (M2TS) packets after a 4-byte timestamp.
TS_SYNC_BYTE = 0x47
TS_LAYOUTS = ((0, 188), (4, 192))


def probe_languages(file: Path) -> tuple[str, list[str]] | None:
    media_info = MediaInfo.parse(file, parse_speed=0.25)
    if not any(t.track_type == "Video" for t in media_info.tracks):
        return None
    audio = [t for t in media_info.tracks if t.track_type == "Audio"]
    if not audio:
        return None

    def lang(track) -> str:
        return (track.language or "und").lower()

    default = next((t for t in audio if t.default == "Yes"), audio[0])
    return lang(default), sorted({lang(t) for t in audio})


def is_english(lang: str) -> bool:
    return lang.split("-", 1)[0] in ENGLISH


def needs_attention(default_lang: str, languages: list[str]) -> bool:
    if not is_english(default_lang):
        return True
    return any(not is_english(lang) for lang in languages)


def is_transport_stream(head: bytes) -> bool:
    """True if head starts like a transport stream (first two sync bytes)."""
    return any(
        len(head) > offset
        and head[offset] == TS_SYNC_BYTE
        and (len(head) <= offset + size or head[offset + size] == TS_SYNC_BYTE)
        for offset, size in TS_LAYOUTS
    )


def is_video(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        return False
    if suffix not in TRANSPORT_STREAM_EXTENSIONS:
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(max(offset + size for offset, size in TS_LAYOUTS) + 1)
    except OSError:
        return True  # let the probe report the error
    return is_transport_stream(head)


def walk_files(directory: Path, errors: list[OSError]) -> list[Path]:
    files = []
    for root, _dirs, names in os.walk(directory, onerror=errors.append):
        for name in names:
            path = Path(root) / name
            if is_video(path):
                files.append(path)
    return files


def main() -> int:
    if len(sys.argv) < 2:
        status(f"USAGE: {Path(sys.argv[0]).name} DIR [DIR ...]")
        return 2
    if not MediaInfo.can_parse():
        status("ERROR: the libmediainfo library was not found")
        return 1
    workers = positive_int(WORKERS_VARIABLE, DEFAULT_WORKERS)
    rows = []
    errors = 0
    scanned = 0
    skipped = 0

    def report_error(message):
        nonlocal errors
        errors += 1
        if sys.stderr.isatty():
            print("\r\033[K", end="", file=sys.stderr)
        status(f"ERROR: {message}")

    # Show each file relative to the directory argument it was found under.
    display: dict[Path, str] = {}
    seen = set()
    reported_walk_errors: set[str] = set()
    for argument in sys.argv[1:]:
        try:
            directory = Path(argument).resolve()
        except OSError as exc:
            report_error(f"{argument}: {exc}")
            continue
        if directory in seen:
            continue
        seen.add(directory)
        if not directory.is_dir():
            report_error(f"not a directory: {argument}")
            continue

        def shown(path: Path, argument=argument, directory=directory) -> str:
            return str(Path(argument) / path.relative_to(directory))

        walk_errors: list[OSError] = []
        with spinner(f"Scanning {argument}"):
            found = walk_files(directory, walk_errors)
        # Overlapping arguments walk the same directories; report each once.
        for exc in walk_errors:
            key = exc.filename or str(exc)
            if key in reported_walk_errors:
                continue
            reported_walk_errors.add(key)
            if exc.filename:
                report_error(f"{shown(Path(exc.filename))}: {exc.strerror or exc}")
            else:
                report_error(str(exc))
        for file in found:
            display.setdefault(file, shown(file))

    files = sorted(display)
    total = len(files)
    status(f"Found {total} file(s) to check")
    is_tty = sys.stderr.isatty()
    done = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(probe_languages, file): file for file in files}
        for future in as_completed(futures):
            file = futures[future]
            done += 1
            if is_tty:
                print(f"\r[*]: [{done}/{total}]", end="", file=sys.stderr)
            try:
                probed = future.result()
            except Exception as exc:  # noqa: BLE001 - keep scanning
                # One unreadable file should not end the whole scan.
                report_error(f"{display[file]}: {exc}")
                continue
            if probed is None:
                skipped += 1
                continue
            scanned += 1
            default_lang, languages = probed
            if needs_attention(default_lang, languages):
                rows.append((default_lang, ",".join(languages), display[file]))
    finally:
        # Don't wait for in-flight probes (e.g. on Ctrl-C); run() exits the
        # process with os._exit, which does not join worker threads.
        pool.shutdown(wait=False, cancel_futures=True)
        if is_tty:
            print("\r\033[K", end="", file=sys.stderr)
    rows.sort(key=lambda row: row[2])

    if rows:
        table = tabulate(
            rows, headers=["Default", "Languages", "File"], tablefmt="simple"
        )
        print(f"\n{table}\n", flush=True)

    status(
        f"Scanned {scanned} video file(s), skipped {skipped} without video or audio, flagged {len(rows)}."
    )
    return 1 if errors else 0


if __name__ == "__main__":
    run(main)
