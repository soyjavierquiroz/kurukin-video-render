import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# 测试文件直接运行时，也能从仓库根目录导入 app 包。
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import subtitle


def _srt_block(index, start, end, text):
    return f"{index}\n{start} --> {end}\n{text}\n\n"


def _write_srt(path, blocks):
    path.write_text("".join(blocks), encoding="utf-8")


def _read_report(subtitle_file):
    report_file = subtitle_file.with_name(f"{subtitle_file.stem}-alignment.json")
    return json.loads(report_file.read_text(encoding="utf-8"))


class TestSubtitleService(unittest.TestCase):
    def test_file_to_subtitles_returns_empty_for_missing_input(self):
        """空路径和不存在的文件都应安全返回空列表。"""
        self.assertEqual(subtitle.file_to_subtitles(""), [])
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_file = Path(tmp_dir) / "missing.srt"
            self.assertEqual(subtitle.file_to_subtitles(str(missing_file)), [])

    def test_levenshtein_distance_and_similarity_cover_common_boundaries(self):
        """
        字幕校正依赖编辑距离选择是否继续合并相邻字幕，因此覆盖空字符串、
        参数交换、大小写忽略和明显不相似四种边界，防止算法调整后误合并。
        """
        self.assertEqual(subtitle.levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(subtitle.levenshtein_distance("a", "longer"), 6)
        self.assertEqual(subtitle.levenshtein_distance("hello", ""), 5)
        self.assertEqual(subtitle.similarity("Hello", "hello"), 1.0)
        self.assertLess(subtitle.similarity("hello", "world"), 0.5)

    def test_create_returns_empty_when_whisper_is_unavailable(self):
        """可选 Whisper 依赖未安装时应跳过，而不是在任务线程中抛异常。"""
        with patch.object(subtitle, "WhisperModel", None):
            self.assertEqual(subtitle.create("audio.mp3"), "")

    def test_create_returns_none_when_whisper_model_cannot_load(self):
        """模型下载或初始化失败时必须返回失败结果，并允许任务层更新状态。"""
        with patch.object(subtitle, "model", None), patch.object(
            subtitle,
            "WhisperModel",
            side_effect=RuntimeError("model unavailable"),
        ):
            self.assertIsNone(subtitle.create("audio.mp3"))

    def test_create_writes_punctuated_and_trailing_segments(self):
        """
        使用假的 Whisper 模型覆盖逐词时间戳处理，不访问网络也不加载真实模型。
        一个 segment 同时包含标点断句和末尾无标点文本，可验证两条关键写入路径。
        """

        class _FakeWhisperModel:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs

            def transcribe(self, audio_file, **kwargs):
                words = [
                    SimpleNamespace(start=0.0, end=0.4, word="Hello"),
                    SimpleNamespace(start=0.4, end=0.9, word=" world."),
                    SimpleNamespace(start=1.0, end=1.5, word="Again"),
                ]
                segment = SimpleNamespace(
                    start=0.0,
                    end=1.8,
                    words=words,
                )
                info = SimpleNamespace(language="en", language_probability=0.99)
                return [segment], info

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "generated.srt"
            with patch.object(subtitle, "model", None), patch.object(
                subtitle,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                subtitle.create("audio.mp3", str(subtitle_file))

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello world", "Again"])

    def test_correct_ignores_markdown_separator_lines(self):
        """
        Whisper fallback 校正阶段也必须忽略 `---` 这类不可发声脚本行。

        如果这里继续保留 Markdown 分隔符，`correct()` 会认为脚本行数多于
        字幕行数，并补出 `00:00:00,000 --> 00:00:00,000`，剪辑软件会把
        生成的 SRT 判定为不可导入。
        """
        original_srt = (
            "1\n"
            "00:00:00,100 --> 00:00:01,000\n"
            "第一段\n\n"
            "2\n"
            "00:00:01,100 --> 00:00:02,000\n"
            "第二段\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(
                subtitle_file=str(subtitle_file),
                video_script="第一段\n---\n第二段",
            )

            corrected_srt = subtitle_file.read_text(encoding="utf-8")

        self.assertIn("第一段", corrected_srt)
        self.assertIn("第二段", corrected_srt)
        self.assertNotIn("---", corrected_srt)
        self.assertNotIn("00:00:00,000 --> 00:00:00,000", corrected_srt)

    def test_correct_exact_match_uses_script_text_and_writes_report(self):
        original_srt = _srt_block(
            1,
            "00:00:00,100",
            "00:00:02,100",
            "el nino penso una relacion dificil",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(subtitle_file, [original_srt])

            report = subtitle.correct(
                str(subtitle_file), "El niño pensó una relación difícil."
            )
            items = subtitle.file_to_subtitles(str(subtitle_file))
            report_from_disk = _read_report(subtitle_file)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][2], "El niño pensó una relación difícil.")
        self.assertEqual(report["status"], "ok")
        self.assertAlmostEqual(report["confidence"], 1.0)
        self.assertFalse(report["review_required"])
        self.assertEqual(report_from_disk["status"], "ok")

    def test_correct_handles_omitted_word_when_global_coverage_is_high(self):
        original_srt = _srt_block(
            1,
            "00:00:00,000",
            "00:00:05,000",
            "Esas personas adultas ahora deciden con calma sobre su futuro",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(subtitle_file, [original_srt])

            report = subtitle.correct(
                str(subtitle_file),
                "Esas personas ya adultas ahora deciden con calma sobre su futuro.",
            )
            items = subtitle.file_to_subtitles(str(subtitle_file))
            final_srt = subtitle_file.read_text(encoding="utf-8")

        self.assertEqual(report["status"], "ok")
        self.assertGreaterEqual(report["confidence"], subtitle.GLOBAL_OK_THRESHOLD)
        self.assertEqual(
            items[0][2],
            "Esas personas ya adultas ahora deciden con calma sobre su futuro.",
        )
        self.assertNotIn("00:00:00,000 --> 00:00:00,000", final_srt)

    def test_correct_wrong_word_does_not_shift_later_alignment(self):
        original_srt = _srt_block(
            1,
            "00:00:00,000",
            "00:00:06,000",
            "Ella sufrio abandono pero luego encontro apoyo estable y digno",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(subtitle_file, [original_srt])

            report = subtitle.correct(
                str(subtitle_file),
                "Ella sufrió abandonó pero luego encontró apoyo estable y digno.",
            )

        self.assertEqual(report["status"], "ok")
        self.assertGreaterEqual(report["lines"][0]["coverage"], 0.9)

    def test_correct_added_whisper_word_keeps_later_alignment(self):
        original_srt = _srt_block(
            1,
            "00:00:00,000",
            "00:00:06,000",
            "Hoy muy temprano revisamos el plan completo con calma",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(subtitle_file, [original_srt])

            report = subtitle.correct(
                str(subtitle_file), "Hoy temprano revisamos el plan completo con calma."
            )
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(report["status"], "ok")
        self.assertEqual(items[0][2], "Hoy temprano revisamos el plan completo con calma.")

    def test_correct_combines_two_whisper_subtitles_for_one_script_line(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(
                subtitle_file,
                [
                    _srt_block(1, "00:00:00,000", "00:00:01,000", "Hello"),
                    _srt_block(2, "00:00:01,000", "00:00:02,000", "world"),
                ],
            )

            report = subtitle.correct(str(subtitle_file), "Hello world.")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1], "00:00:00,000 --> 00:00:02,000")
        self.assertEqual(items[0][2], "Hello world.")

    def test_correct_splits_one_whisper_subtitle_across_two_script_lines(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(
                subtitle_file,
                [_srt_block(1, "00:00:00,000", "00:00:04,000", "First line Second line")],
            )

            report = subtitle.correct(str(subtitle_file), "First line. Second line.")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(report["status"], "ok")
        self.assertEqual([item[2] for item in items], ["First line.", "Second line."])
        self.assertEqual(items[0][1], "00:00:00,000 --> 00:00:02,000")
        self.assertEqual(items[1][1], "00:00:02,000 --> 00:00:04,000")

    def test_correct_low_confidence_keeps_raw_whisper_and_requires_review(self):
        original_srt = _srt_block(
            1,
            "00:00:00,100",
            "00:00:01,000",
            "Completely different audio",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(subtitle_file, [original_srt])

            report = subtitle.correct(
                str(subtitle_file),
                "Expected sentence with many unrelated canonical script words.",
            )
            final_srt = subtitle_file.read_text(encoding="utf-8")

        self.assertEqual(report["status"], "review_required")
        self.assertTrue(report["review_required"])
        self.assertEqual(final_srt, original_srt)
        self.assertNotIn("Expected sentence", final_srt)

    def test_correct_writes_exact_raw_backup(self):
        original_srt = _srt_block(1, "00:00:00,000", "00:00:01,000", "Hello world")

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            raw_file = Path(tmp_dir) / "subtitle.raw.srt"
            _write_srt(subtitle_file, [original_srt])

            subtitle.correct(str(subtitle_file), "Hello world.")

            self.assertEqual(raw_file.read_text(encoding="utf-8"), original_srt)

    def test_correct_outputs_monotonic_timings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            _write_srt(
                subtitle_file,
                [_srt_block(1, "00:00:00,000", "00:00:04,000", "One two three four")],
            )

            report = subtitle.correct(str(subtitle_file), "One two. Three four.")

        self.assertEqual(report["status"], "ok")
        previous_end = 0
        for line in report["lines"]:
            self.assertIsNotNone(line["start"])
            self.assertIsNotNone(line["end"])
            self.assertGreater(line["end"], line["start"])
            self.assertGreaterEqual(line["start"], previous_end)
            previous_end = line["end"]

    def test_file_to_subtitles_keeps_last_block_without_trailing_newline(self):
        """
        The final subtitle must be parsed even when the SRT file does not end
        with a trailing blank line. Many tools omit it, and previously the last
        block was silently dropped because only a blank line flushed a block.
        """
        srt_without_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_without_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][2], "Hello")
        self.assertEqual(items[1][2], "World")

    def test_file_to_subtitles_parses_blocks_with_trailing_newline(self):
        """A normal SRT ending in a blank line still parses all blocks."""
        srt_with_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_with_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello", "World"])


if __name__ == "__main__":
    unittest.main()
