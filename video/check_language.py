#!/usr/bin/env python3

import mimetypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pymediainfo import MediaInfo
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.output import spinner, status
from lib.runtime import run

ENGLISH = {"en", "eng"}
mimetypes.add_type("video/mp2t", ".m2ts")

try:
    WORKERS = max(1, int(os.environ.get("WORKERS", "8")))
except ValueError:
    WORKERS = 8


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


def walk_files(directory: Path, errors: list[str]) -> list[Path]:
    files = []
    for root, _dirs, names in os.walk(
        directory, onerror=lambda e: errors.append(str(e))
    ):
        for name in names:
            if (mimetypes.guess_type(name)[0] or "").startswith("video/"):
                files.append(Path(root) / name)
    return files


def main() -> int:
    if len(sys.argv) < 2:
        status(f"USAGE: {Path(sys.argv[0]).name} DIR [DIR ...]")
        return 2
    if not MediaInfo.can_parse():
        status("ERROR: the libmediainfo library was not found")
        return 1
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

    files = []
    seen = set()
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
        walk_errors: list[str] = []
        with spinner(f"Scanning {directory}"):
            files.extend(walk_files(directory, walk_errors))
        for message in walk_errors:
            report_error(message)

    files = sorted(set(files))
    total = len(files)
    status(f"Found {total} file(s) to check")
    cwd = Path.cwd()
    is_tty = sys.stderr.isatty()
    done = 0
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futures = {pool.submit(probe_languages, file): file for file in files}
        for future in as_completed(futures):
            file = futures[future]
            done += 1
            if is_tty:
                print(f"\r[*]: [{done}/{total}]", end="", file=sys.stderr)
            try:
                probed = future.result()
            except (OSError, RuntimeError, ValueError) as exc:
                report_error(f"{file}: {exc}")
                continue
            if probed is None:
                skipped += 1
                continue
            scanned += 1
            default_lang, languages = probed
            if needs_attention(default_lang, languages):
                try:
                    label = str(file.relative_to(cwd))
                except ValueError:
                    label = str(file)
                rows.append((label, default_lang, ",".join(languages) or "-"))
    finally:
        pool.shutdown(cancel_futures=True)
        if is_tty:
            print("\r\033[K", end="", file=sys.stderr)
    rows.sort()

    if rows:
        table = tabulate(
            rows, headers=["File", "Default", "Languages"], tablefmt="simple"
        )
        print(f"\n{table}\n", flush=True)

    status(
        f"Scanned {scanned} video file(s), skipped {skipped} without video or audio, flagged {len(rows)}."
    )
    return 1 if errors else 0


if __name__ == "__main__":
    run(main)
