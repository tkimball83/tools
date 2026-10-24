#!/usr/bin/env python3
"""Assert-based self-checks: venv/bin/python test_tools.py"""

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_language = load(ROOT / "video" / "check_language.py")
compare = load(ROOT / "rpm" / "compare.py")

assert check_language.is_english("en")
assert check_language.is_english("en-us")
assert check_language.is_english("eng")
assert not check_language.is_english("und")
assert not check_language.is_english("fre")

assert not check_language.needs_attention("eng", ["eng"])
assert check_language.needs_attention("fre", ["eng", "fre"])
assert check_language.needs_attention("eng", ["eng", "jpn"])
assert check_language.needs_attention("und", [])


class Track:
    def __init__(self, track_type, language=None, default="No"):
        self.track_type = track_type
        self.language = language
        self.default = default


class FakeMediaInfo:
    tracks = ()

    @classmethod
    def parse(cls, *_, **__):
        return cls


check_language.MediaInfo = FakeMediaInfo

FakeMediaInfo.tracks = [Track("Audio", "eng")]
assert check_language.probe_languages(Path("x")) is None  # no video track

FakeMediaInfo.tracks = [Track("Video")]
assert check_language.probe_languages(Path("x")) is None  # no audio track

FakeMediaInfo.tracks = [
    Track("Video"),
    Track("Audio", "jpn"),
    Track("Audio", "ENG", default="Yes"),
]
assert check_language.probe_languages(Path("x")) == ("eng", ["eng", "jpn"])

FakeMediaInfo.tracks = [Track("Video"), Track("Audio"), Track("Audio")]
assert check_language.probe_languages(Path("x")) == ("und", ["und"])


def fake_run(stdout, returncode=0):
    def runner(*_, **__):
        return subprocess.CompletedProcess([], returncode, stdout, "")

    return runner


FakeMediaInfo.can_parse = classmethod(lambda *_: False)
check_language.status = lambda *_: None
check_language.sys.argv = ["check_language.py", "."]
assert check_language.main() == 1  # missing libmediainfo fails fast

compare.subprocess.run = fake_run(
    "pkg.x86_64\t1.0-1\nepoch.noarch\t0:2.0-3\ngpg-pubkey.(none)\tabc-def\n"
)
assert compare.gather_packages("host") == {
    "pkg.x86_64": {"1.0-1"},
    "epoch.noarch": {"2.0-3"},
}

assert sorted(
    ["5.14.0-70.el9", "5.14.0-611.el9", "5.14.0-570.el9"], key=compare.version_key
) == ["5.14.0-70.el9", "5.14.0-570.el9", "5.14.0-611.el9"]

compare.subprocess.run = fake_run("garbage without a tab\n")
try:
    compare.gather_packages("host")
    raise AssertionError("malformed output should raise")
except RuntimeError:
    pass


def timeout_run(*_, **__):
    raise subprocess.TimeoutExpired("ssh", compare.SSH_TIMEOUT)


compare.subprocess.run = timeout_run
try:
    compare.gather_packages("host")
    raise AssertionError("timeout should raise")
except RuntimeError as exc:
    assert str(exc) == f"timed out after {compare.SSH_TIMEOUT}s"

compare.subprocess.run = fake_run("", returncode=255)
try:
    compare.gather_packages("host")
    raise AssertionError("ssh failure should raise")
except RuntimeError:
    pass

print("[*]: all checks passed")
