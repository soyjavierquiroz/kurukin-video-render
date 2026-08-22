"""Production regressions for canonical Spanish semantic subtitle cues."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.custom.subtitle_optimizer import parse_srt
from app.services import subtitle
from scripts.produce_batch import subtitle_quality_issues


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
