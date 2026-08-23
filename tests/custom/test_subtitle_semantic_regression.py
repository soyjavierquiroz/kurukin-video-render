"""Production regressions for canonical Spanish semantic subtitle cues."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.custom.subtitle_optimizer import parse_srt
from app.services import subtitle
from scripts import produce_batch
from scripts.produce_batch import subtitle_quality_issues, subtitle_semantic_gate, subtitle_validation_issues


SCRIPT = """Pero nadie conoce todo lo que ocurrió antes de que esa hija dijera.
Ella hizo lo que pudo con su historia.
Comprenderla no te obliga a exponerte nuevamente al mismo dolor.
Y un límite no siempre es un castigo."""

RAW = """1
00:00:00,000 --> 00:00:01,000
Pero nadie conoce todo lo

2
00:00:01,000 --> 00:00:02,000
que ocurrió antes de que esa hija dijera.

3
00:00:02,000 --> 00:00:03,000
Ella hizo lo

4
00:00:03,000 --> 00:00:04,000
que pudo con su historia.

5
00:00:04,000 --> 00:00:05,000
Comprenderla no te obliga a

6
00:00:05,000 --> 00:00:06,000
exponerte nuevamente al mismo dolor.

7
00:00:06,000 --> 00:00:07,000
Y un límite no

8
00:00:07,000 --> 00:00:08,000
siempre es un castigo.
"""


class SubtitleSemanticRegressionTests(unittest.TestCase):
    def test_rebalances_real_dangling_span_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subtitle.srt"
            path.write_text(RAW, encoding="utf-8")
            self.assertTrue(any("dangling" in issue for issue in subtitle_quality_issues(path, 8.0)))
            subtitle.correct(str(path), SCRIPT)
            cues = [item["text"] for item in parse_srt(path.read_text(encoding="utf-8"))]
            self.assertEqual(subtitle_quality_issues(path, 8.0), [])

        self.assertIn("Ella hizo lo que pudo con su historia.", cues)
        self.assertIn("Comprenderla no te obliga a exponerte nuevamente al mismo dolor.", cues)
        self.assertIn("Y un límite no siempre es un castigo.", cues)
        self.assertFalse(any(text in cues for text in (
            "Pero nadie conoce todo lo", "Ella hizo lo", "Comprenderla no te obliga a", "Y un límite no",
        )))

    def test_repairable_dangling_connector_is_repaired_without_structural_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subtitle.srt"
            path.write_text(RAW, encoding="utf-8")
            gate = subtitle_semantic_gate(path, SCRIPT, 8.0)

            self.assertEqual(gate["structural_issues"], [])
            self.assertTrue(gate["repaired"])
            self.assertEqual(gate["warnings"], [])
            self.assertEqual(subtitle_quality_issues(path, 8.0), [])

    def test_unrepaired_style_warning_is_accepted_by_the_semantic_gate(self):
        raw = (
            "1\n00:00:00,000 --> 00:00:01,000\nUna frase que\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\ncontinúa aquí.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subtitle.srt"
            path.write_text(raw, encoding="utf-8")
            original_repair = produce_batch.repair_subtitle_semantics
            produce_batch.repair_subtitle_semantics = lambda *_args: {"status": "ok", "confidence": 1.0}
            try:
                gate = subtitle_semantic_gate(path, "Una frase que continúa aquí.", 2.0)
            finally:
                produce_batch.repair_subtitle_semantics = original_repair

            self.assertEqual(gate["structural_issues"], [])
            self.assertEqual(gate["warnings"], ["cue_1_dangling_que"])
            self.assertTrue(gate["repaired"])

    def test_punctuated_interrogative_que_is_not_a_dangling_connector(self):
        raw = (
            "1\n00:00:00,000 --> 00:00:01,000\n¿Por qué?\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nLa siguiente frase.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subtitle.srt"
            path.write_text(raw, encoding="utf-8")
            structural, semantic = subtitle_validation_issues(path, 2.0)
            self.assertEqual(structural, [])
            self.assertNotIn("cue_1_dangling_que", semantic)

    def test_structural_timestamp_corruption_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subtitle.srt"
            path.write_text("1\nnot a timestamp\nTexto\n", encoding="utf-8")
            structural, semantic = subtitle_validation_issues(path, 1.0)
            self.assertTrue(structural)
            self.assertEqual(semantic, [])

    def test_empty_cue_and_outside_audio_are_structural_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.srt"
            empty.write_text("1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8")
            self.assertIn("cue_1_empty", subtitle_validation_issues(empty, 1.0)[0])

            outside = Path(tmp) / "outside.srt"
            outside.write_text("1\n00:00:00,000 --> 00:00:02,000\nTexto\n", encoding="utf-8")
            self.assertIn("cue_1_outside_audio", subtitle_validation_issues(outside, 1.0)[0])
