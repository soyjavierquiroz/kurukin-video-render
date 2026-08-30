import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import local_job_wrapper


def make_spec(asset_files=None):
    if asset_files is None:
        asset_files = [
            {"file": "clip-02.mp4", "label": "support", "order": 2},
            {"file": "clip-01.mp4", "label": "intro", "order": 1},
        ]
    return {
        "job_id": "relaciones-local-demo-001",
        "description": "Demo usando assets locales seleccionados",
        "selectedAssets": asset_files,
        "video": {
            "video_subject": "La importancia de escoger bien a tu pareja",
            "video_script": (
                "Escoger bien a tu pareja puede cambiar por completo el rumbo "
                "de tu vida."
            ),
            "video_aspect": "9:16",
            "video_concat_mode": "sequential",
            "video_transition_mode": "None",
            "video_clip_duration": 3,
            "video_count": 1,
            "voice_name": "es-MX-DaliaNeural-Female",
            "voice_volume": 1.0,
            "voice_rate": 1.0,
            "bgm_type": "none",
            "bgm_volume": 0.2,
            "subtitle_enabled": True,
            "subtitle_position": "bottom",
            "font_size": 60,
            "stroke_color": "#000000",
            "stroke_width": 1.5,
            "n_threads": 2,
            "paragraph_number": 1,
        },
    }


def make_preset_spec():
    spec = make_spec()
    spec["subtitle_style_preset"] = "clean_center_bold"
    return spec


class TestLocalJobWrapper(unittest.TestCase):
    def write_spec(self, directory, spec):
        spec_path = Path(directory) / "job.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return spec_path

    def write_assets(self, directory, filenames):
        assets_dir = Path(directory) / "local_videos"
        assets_dir.mkdir()
        for filename in filenames:
            (assets_dir / filename).write_text("dummy", encoding="utf-8")
        return assets_dir

    def write_fonts(self, directory, filenames):
        fonts_dir = Path(directory) / "fonts"
        fonts_dir.mkdir()
        for filename in filenames:
            (fonts_dir / filename).write_text("dummy", encoding="utf-8")
        return fonts_dir

    def test_build_pending_job_generates_local_materials(self):
        spec = make_spec()
        ordered_assets = [
            {"file": "clip-01.mp4", "label": "intro", "order": 1},
            {"file": "clip-02.mp4", "label": "support", "order": 2},
        ]

        pending_job = local_job_wrapper.build_pending_job(spec, ordered_assets)

        self.assertEqual(pending_job["video_source"], "local")
        self.assertEqual(
            pending_job["video_materials"],
            [
                {"provider": "local", "url": "clip-01.mp4", "duration": 0},
                {"provider": "local", "url": "clip-02.mp4", "duration": 0},
            ],
        )
        self.assertNotIn("selectedAssets", pending_job)

    def test_build_pending_job_applies_clean_center_bold_preset(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fonts_dir = self.write_fonts(tmp_dir, ["BeVietnamPro-Bold.ttf"])
            spec = make_preset_spec()
            ordered_assets = [
                {"file": "clip-01.mp4", "label": "intro", "order": 1},
                {"file": "clip-02.mp4", "label": "support", "order": 2},
            ]

            pending_job = local_job_wrapper.build_pending_job(
                spec,
                ordered_assets,
                fonts_dir=fonts_dir,
            )

        self.assertFalse(pending_job["text_background_color"])
        self.assertEqual(pending_job["subtitle_position"], "center")
        self.assertEqual(pending_job["stroke_width"], 3)
        self.assertEqual(pending_job["font_name"], "BeVietnamPro-Bold.ttf")
        self.assertIn("resolved_subtitle_style", pending_job["runner"])
        self.assertEqual(
            pending_job["runner"]["resolved_subtitle_style"]["font_name"],
            "BeVietnamPro-Bold.ttf",
        )
        self.assertNotIn("subtitle_style_preset", pending_job)
        self.assertNotIn("subtitle_style_overrides", pending_job)

    def test_build_pending_job_without_preset_keeps_previous_subtitle_values(self):
        spec = make_spec()
        ordered_assets = [
            {"file": "clip-01.mp4", "label": "intro", "order": 1},
            {"file": "clip-02.mp4", "label": "support", "order": 2},
        ]

        pending_job = local_job_wrapper.build_pending_job(spec, ordered_assets)

        self.assertEqual(pending_job["subtitle_position"], "bottom")
        self.assertEqual(pending_job["font_size"], 60)
        self.assertEqual(pending_job["stroke_width"], 1.5)
        self.assertNotIn("resolved_subtitle_style", pending_job["runner"])

    def test_selected_assets_are_ordered_by_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = self.write_assets(tmp_dir, ["clip-01.mp4", "clip-02.mp4"])
            ordered_assets = local_job_wrapper.validate_job_spec(
                make_spec(),
                local_videos_dir=assets_dir,
                skip_media_probe=True,
            )

        self.assertEqual(
            [asset["file"] for asset in ordered_assets],
            ["clip-01.mp4", "clip-02.mp4"],
        )

    def test_rejects_parent_path_asset(self):
        spec = make_spec([{"file": "../clip-01.mp4"}])
        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            local_job_wrapper.validate_job_spec(
                spec,
                local_videos_dir=tempfile.gettempdir(),
                skip_media_probe=True,
            )

    def test_rejects_duplicate_asset_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = self.write_assets(tmp_dir, ["clip-01.mp4"])
            spec = make_spec(
                [
                    {"file": "clip-01.mp4", "order": 1},
                    {"file": "clip-01.mp4", "order": 2},
                ]
            )

            with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
                local_job_wrapper.validate_job_spec(
                    spec,
                    local_videos_dir=assets_dir,
                    skip_media_probe=True,
                )

    def test_validate_only_skip_media_probe_accepts_dummy_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = self.write_assets(tmp_dir, ["clip-01.mp4", "clip-02.mp4"])
            spec_path = self.write_spec(tmp_dir, make_spec())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = local_job_wrapper.main(
                    [
                        str(spec_path),
                        "--validate-only",
                        "--local-videos-dir",
                        str(assets_dir),
                        "--skip-media-probe",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertIn("OK: job spec is valid", stdout.getvalue())

    def test_enqueue_writes_pending_json_without_root_selected_assets(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = self.write_assets(tmp_dir, ["clip-01.mp4", "clip-02.mp4"])
            queue_dir = Path(tmp_dir) / "nightly_jobs"
            spec_path = self.write_spec(tmp_dir, make_spec())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = local_job_wrapper.main(
                    [
                        str(spec_path),
                        "--enqueue",
                        "--queue-dir",
                        str(queue_dir),
                        "--local-videos-dir",
                        str(assets_dir),
                        "--skip-media-probe",
                    ]
                )
            pending_files = list((queue_dir / "pending").glob("*.json"))
            payload = json.loads(pending_files[0].read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(pending_files), 1)
        self.assertNotIn("selectedAssets", payload)
        self.assertIn("selectedAssets", payload["runner"])

    def test_print_payload_outputs_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = self.write_assets(tmp_dir, ["clip-01.mp4", "clip-02.mp4"])
            spec_path = self.write_spec(tmp_dir, make_spec())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = local_job_wrapper.main(
                    [
                        str(spec_path),
                        "--print-payload",
                        "--local-videos-dir",
                        str(assets_dir),
                        "--skip-media-probe",
                    ]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["video_source"], "local")
        self.assertEqual(payload["video_materials"][0]["url"], "clip-01.mp4")


if __name__ == "__main__":
    unittest.main()
