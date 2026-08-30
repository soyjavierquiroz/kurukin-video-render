import unittest
from unittest.mock import patch

from app.custom.mpt_defaults import MptDefaultsError, mpt_video_params, resolve_effective_mpt_settings


class MptDefaultsTests(unittest.TestCase):
    def test_no_defaults_preserves_approved_batch_baseline(self):
        settings = resolve_effective_mpt_settings()
        self.assertEqual(settings["bgm"], {"mode": "NONE", "volume": 0.0, "file_id": "", "prompt": ""})
        self.assertEqual(settings["video_aspect"], "9:16")
        self.assertEqual(settings["video_clip_duration"], 5)

    def test_random_maps_to_native_params(self):
        params = mpt_video_params(resolve_effective_mpt_settings({"bgm": {"mode": "random", "volume": .12}}))
        self.assertEqual(params["bgm_type"], "random")
        self.assertEqual(params["bgm_volume"], .12)

    def test_none_wins_over_configured_volume(self):
        params = mpt_video_params(resolve_effective_mpt_settings({"bgm": {"mode": "none", "volume": .12}}))
        self.assertEqual(params["bgm_type"], "")
        self.assertEqual(params["bgm_volume"], 0)

    def test_generated_bgm_providers_pass_native_type_and_prompt(self):
        for mode, native_type in (("SONILO", "sonilo"), ("ELEVENLABS", "elevenlabs")):
            with self.subTest(mode=mode):
                params = mpt_video_params(resolve_effective_mpt_settings({
                    "bgm": {"mode": mode, "volume": .2, "prompt": "calm piano"},
                }))
                self.assertEqual(params["bgm_type"], native_type)
                self.assertEqual(params["video_music_prompt"], "calm piano")

    def test_explicit_zero_volume_is_preserved(self):
        params = mpt_video_params(resolve_effective_mpt_settings({"bgm": {"mode": "RANDOM", "volume": 0}}))
        self.assertEqual(params["bgm_volume"], 0)

    def test_video_capabilities_are_preserved(self):
        params = mpt_video_params(resolve_effective_mpt_settings({
            "video_aspect": "16:9", "video_resolution": "720p",
            "video_clip_duration": 7, "video_transition_mode": "FadeIn",
        }))
        self.assertEqual(params["video_aspect"], "16:9")
        self.assertEqual(params["video_resolution"], "720p")
        self.assertEqual(params["video_clip_duration"], 7)
        self.assertEqual(params["video_transition_mode"], "FadeIn")

    def test_custom_requires_native_allowed_root_resolution(self):
        with self.assertRaisesRegex(MptDefaultsError, "approved MPT BGM roots"):
            resolve_effective_mpt_settings({"bgm": {"mode": "CUSTOM", "file_id": "/etc/passwd"}})

    def test_custom_uses_native_resolved_file_at_render(self):
        defaults = {"bgm": {"mode": "CUSTOM", "file_id": "approved.mp3", "volume": .3}}
        with patch("app.services.bgm.resolve_bgm_file", return_value="/MoneyPrinterTurbo/storage/bgm/approved.mp3"):
            params = mpt_video_params(resolve_effective_mpt_settings(defaults))
        self.assertEqual(params["bgm_type"], "custom")
        self.assertEqual(params["bgm_file"], "/MoneyPrinterTurbo/storage/bgm/approved.mp3")

    def test_unknown_defaults_are_rejected(self):
        with self.assertRaisesRegex(MptDefaultsError, "unsupported mpt_defaults field"):
            resolve_effective_mpt_settings({"video_source": "local"})
