import tempfile
import unittest
from pathlib import Path

from scripts import subtitle_style_presets


class TestSubtitleStylePresets(unittest.TestCase):
    def write_font(self, directory, name):
        path = Path(directory) / name
        path.write_text("dummy", encoding="utf-8")
        return path

    def test_clean_center_bold_resolves_bevietnam_when_montserrat_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.write_font(tmp_dir, "BeVietnamPro-Bold.ttf")

            resolved_preset, overrides, style = (
                subtitle_style_presets.resolve_subtitle_style(
                    "clean_center_bold",
                    None,
                    fonts_dir=tmp_dir,
                )
            )

        self.assertEqual(resolved_preset, "clean_center_bold")
        self.assertEqual(overrides, {})
        self.assertEqual(style["font_name"], "BeVietnamPro-Bold.ttf")
        self.assertEqual(style["subtitle_position"], "center")
        self.assertFalse(style["text_background_color"])

    def test_prefers_montserrat_bold_when_available(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.write_font(tmp_dir, "BeVietnamPro-Bold.ttf")
            self.write_font(tmp_dir, "Montserrat-Bold.ttf")

            _, _, style = subtitle_style_presets.resolve_subtitle_style(
                "clean_center_bold",
                None,
                fonts_dir=tmp_dir,
            )

        self.assertEqual(style["font_name"], "Montserrat-Bold.ttf")

    def test_unknown_preset_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.write_font(tmp_dir, "BeVietnamPro-Bold.ttf")

            with self.assertRaises(subtitle_style_presets.SubtitleStylePresetError):
                subtitle_style_presets.resolve_subtitle_style(
                    "missing_preset",
                    None,
                    fonts_dir=tmp_dir,
                )

    def test_valid_overrides_are_applied(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.write_font(tmp_dir, "BeVietnamPro-Bold.ttf")

            _, overrides, style = subtitle_style_presets.resolve_subtitle_style(
                "clean_center_bold",
                {
                    "font_size": 76,
                    "stroke_width": 4,
                    "subtitle_position": "bottom",
                },
                fonts_dir=tmp_dir,
            )

        self.assertEqual(overrides["font_size"], 76)
        self.assertEqual(style["font_size"], 76)
        self.assertEqual(style["stroke_width"], 4)
        self.assertEqual(style["subtitle_position"], "bottom")

    def test_disallowed_override_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.write_font(tmp_dir, "BeVietnamPro-Bold.ttf")

            with self.assertRaises(subtitle_style_presets.SubtitleStylePresetError):
                subtitle_style_presets.resolve_subtitle_style(
                    "clean_center_bold",
                    {"video_source": "pexels"},
                    fonts_dir=tmp_dir,
                )

    def test_missing_fallback_font_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(
                subtitle_style_presets.SubtitleStylePresetError,
                "no subtitle font fallback found",
            ):
                subtitle_style_presets.resolve_subtitle_style(
                    "clean_center_bold",
                    None,
                    fonts_dir=tmp_dir,
                )


if __name__ == "__main__":
    unittest.main()
