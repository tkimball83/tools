import contextlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, captured_output, load

check_language = load(ROOT / "video" / "check_language.py")


class Track:
    def __init__(self, track_type, language=None, default="No"):
        self.track_type = track_type
        self.language = language
        self.default = default


ENGLISH = [Track("Video"), Track("Audio", "eng")]
FRENCH = [Track("Video"), Track("Audio", "fre")]


def fake_media_info(tracks_by_name, can_parse=True, delays=None):
    """A MediaInfo stand-in; a track list that is an exception is raised."""

    class FakeMediaInfo:
        @staticmethod
        def can_parse():
            return can_parse

        @staticmethod
        def parse(file, **_):
            time.sleep((delays or {}).get(Path(file).name, 0))
            tracks = tracks_by_name[Path(file).name]
            if isinstance(tracks, BaseException):
                raise tracks
            return mock.Mock(tracks=tracks)

    return FakeMediaInfo


class LanguageTests(unittest.TestCase):
    def test_is_english(self):
        for lang in ("en", "en-us", "eng"):
            self.assertTrue(check_language.is_english(lang), lang)
        for lang in ("und", "fre"):
            self.assertFalse(check_language.is_english(lang), lang)

    def test_needs_attention(self):
        self.assertFalse(check_language.needs_attention("eng", ["eng"]))
        self.assertTrue(check_language.needs_attention("fre", ["eng", "fre"]))
        self.assertTrue(check_language.needs_attention("eng", ["eng", "jpn"]))
        self.assertTrue(check_language.needs_attention("und", []))

    def probe(self, tracks):
        fake = fake_media_info({"x": tracks})
        with mock.patch.object(check_language, "MediaInfo", fake):
            return check_language.probe_languages(Path("x"))

    def test_probe_languages(self):
        self.assertIsNone(self.probe([Track("Audio", "eng")]))  # no video
        self.assertIsNone(self.probe([Track("Video")]))  # no audio
        self.assertEqual(
            self.probe(
                [Track("Video"), Track("Audio", "jpn"), Track("Audio", "ENG", "Yes")]
            ),
            ("eng", ["eng", "jpn"]),
        )
        self.assertEqual(
            self.probe([Track("Video"), Track("Audio"), Track("Audio")]),
            ("und", ["und"]),
        )


class IsVideoTests(unittest.TestCase):
    def test_is_video(self):
        packet = b"\x47" + bytes(187)
        files = {
            "movie.mkv": b"",
            "notes.txt": b"",
            "stream.ts": packet * 2,
            "short.ts": packet[:10],
            "app.ts": b"import x from 'y';\n",
            "Gtext.ts": b"G" + b"x" * 300,
            "clip.MTS": (bytes(4) + b"\x47" + bytes(187)) * 2,
            "module.mts": b"export const x = 1;\n",
            "old.flv": b"",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, data in files.items():
                (Path(tmp) / name).write_bytes(data)
            found = {
                name for name in files if check_language.is_video(Path(tmp) / name)
            }
        self.assertEqual(
            found, {"movie.mkv", "stream.ts", "short.ts", "clip.MTS", "old.flv"}
        )


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.chdir(self.tmp))

    def touch(self, *names):
        for name in names:
            path = self.tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def run_main(self, args, tracks_by_name=None, can_parse=True, delays=None):
        fake = fake_media_info(tracks_by_name or {}, can_parse, delays)
        with (
            mock.patch.object(check_language, "MediaInfo", fake),
            mock.patch.object(sys, "argv", ["check_language.py", *args]),
            captured_output() as (stdout, stderr),
        ):
            code = check_language.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def flagged(self, stdout):
        return [line.split()[-1] for line in stdout.splitlines() if ".mkv" in line]

    def test_missing_libmediainfo_fails_fast(self):
        code, _, stderr = self.run_main(["."], can_parse=False)
        self.assertEqual(code, 1)
        self.assertIn("libmediainfo", stderr)

    def test_results_are_sorted_whatever_order_probes_finish(self):
        self.touch("a.mkv", "b.mkv", "c.mkv")
        tracks = dict.fromkeys(("a.mkv", "b.mkv", "c.mkv"), FRENCH)
        code, stdout, _ = self.run_main(["."], tracks, delays={"a.mkv": 0.3})
        self.assertEqual(code, 0)
        self.assertEqual(self.flagged(stdout), ["a.mkv", "b.mkv", "c.mkv"])

    def test_relative_paths_and_probe_errors(self):
        self.touch("a/x.mkv", "b/x.mkv", "a/ok.mkv", "b/bad.mkv")
        tracks = {"x.mkv": FRENCH, "ok.mkv": ENGLISH, "bad.mkv": KeyError("boom")}
        code, stdout, stderr = self.run_main(["a", "b"], tracks)
        self.assertEqual(code, 1)  # the probe error is reported
        self.assertIn("ERROR: b/bad.mkv: 'boom'", stderr)
        self.assertIn("Scanned 3 video file(s)", stderr)
        self.assertEqual(self.flagged(stdout), ["a/x.mkv", "b/x.mkv"])

    def test_overlapping_and_symlinked_arguments(self):
        self.touch("media/sub/x.mkv")
        (self.tmp / "link").symlink_to("media")
        code, stdout, _ = self.run_main(
            ["link", "media", "media/sub"], {"x.mkv": FRENCH}
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.flagged(stdout), ["link/sub/x.mkv"])  # first wins

    def test_non_directory_argument(self):
        self.touch("file.mkv")
        code, _, stderr = self.run_main(["file.mkv", "missing"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR: not a directory: file.mkv", stderr)
        self.assertIn("ERROR: not a directory: missing", stderr)

    @unittest.skipIf(os.geteuid() == 0, "root can read unreadable directories")
    def test_walk_errors_are_relative_and_reported_once(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        code, _, stderr = self.run_main([".", "locked"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.count("ERROR:"), 1, stderr)
        self.assertIn("ERROR: locked: Permission denied", stderr)


if __name__ == "__main__":
    unittest.main()
