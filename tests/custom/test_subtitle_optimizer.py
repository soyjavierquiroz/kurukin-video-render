import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.subtitle_optimizer import (
    optimize_srt_file,
    parse_srt,
    write_srt,
)


class TestSubtitleOptimizer(unittest.TestCase):
    def test_parse_and_write_simple_srt(self):
        content = (
            "1\n"
            "00:00:00,000 --> 00:00:02,000\n"
            "Hello world\n\n"
        )

        items = parse_srt(content)
        rewritten = write_srt(items)
        reparsed = parse_srt(rewritten)

        self.assertEqual(len(reparsed), 1)
        self.assertEqual(reparsed[0]["text"], "Hello world")
        self.assertEqual(reparsed[0]["start"], 0)
        self.assertEqual(reparsed[0]["end"], 2)

    def test_splits_long_portrait_caption(self):
        content = (
            "1\n"
            "00:00:00,000 --> 00:00:04,000\n"
            "This is a very long caption that should become easier to read on portrait video\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(content, encoding="utf-8")

            result = optimize_srt_file(str(subtitle_file), aspect="9:16")
            items = parse_srt(subtitle_file.read_text(encoding="utf-8"))

        self.assertTrue(result["changed"])
        self.assertGreater(len(items), 1)
        self.assertLessEqual(max(len(item["text"].split()) for item in items), 5)

    def test_timestamps_are_increasing_without_overlaps(self):
        content = (
            "1\n"
            "00:00:01,000 --> 00:00:05,000\n"
            "This is a very long caption that should split into several readable pieces\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(content, encoding="utf-8")

            optimize_srt_file(str(subtitle_file), aspect="9:16")
            items = parse_srt(subtitle_file.read_text(encoding="utf-8"))

        previous_end = None
        for item in items:
            self.assertGreaterEqual(item["start"], 1)
            self.assertLessEqual(item["end"], 5)
            self.assertGreaterEqual(item["end"], item["start"])
            if previous_end is not None:
                self.assertGreaterEqual(item["start"], previous_end)
            previous_end = item["end"]

    def test_short_duration_does_not_split_absurdly(self):
        content = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "This is a very long caption that cannot be split safely in one second\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(content, encoding="utf-8")

            result = optimize_srt_file(str(subtitle_file), aspect="9:16")
            items = parse_srt(subtitle_file.read_text(encoding="utf-8"))

        self.assertEqual(result["optimized_items"], 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["start"], 0)
        self.assertEqual(items[0]["end"], 1)

    def test_backup_is_created_only_when_changed(self):
        unchanged_content = (
            "1\n"
            "00:00:00,000 --> 00:00:02,000\n"
            "Hello world\n\n"
        )
        changed_content = (
            "1\n"
            "00:00:00,000 --> 00:00:04,000\n"
            "This is a very long caption that should create a backup when optimized\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(unchanged_content, encoding="utf-8")

            unchanged_result = optimize_srt_file(str(subtitle_file), aspect="9:16")
            self.assertFalse(unchanged_result["changed"])
            self.assertIsNone(unchanged_result["backup_path"])
            self.assertFalse((Path(tmp_dir) / "subtitle.original.srt").exists())

            subtitle_file.write_text(changed_content, encoding="utf-8")
            changed_result = optimize_srt_file(str(subtitle_file), aspect="9:16")

            self.assertTrue(changed_result["changed"])
            self.assertEqual(
                os.path.basename(changed_result["backup_path"]),
                "subtitle.original.srt",
            )
            self.assertTrue(Path(changed_result["backup_path"]).exists())

    def test_invalid_srt_returns_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text("not an srt file", encoding="utf-8")

            result = optimize_srt_file(str(subtitle_file), aspect="9:16")

        self.assertFalse(result["changed"])
        self.assertEqual(result["original_items"], 0)
        self.assertEqual(result["optimized_items"], 0)
        self.assertIsNone(result["backup_path"])


if __name__ == "__main__":
    unittest.main()
