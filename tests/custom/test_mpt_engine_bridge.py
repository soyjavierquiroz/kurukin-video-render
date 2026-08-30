import builtins
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.mpt_engine_bridge import (
    build_mpt_aroll_broll_task_spec,
    build_validated_mpt_video_task_from_kurukin_job,
    build_mpt_video_task_from_kurukin_job,
    discover_mpt_engine_capabilities,
    get_mpt_video_params_model,
    normalize_mpt_video_params_spec,
    summarize_mpt_task_spec,
    validate_against_mpt_video_params,
    validate_mpt_task_spec,
)


VIDEO_PARAMS_TEST_FIELDS = {
    "video_subject",
    "video_script",
    "video_terms",
    "video_aspect",
    "video_resolution",
    "video_concat_mode",
    "video_clip_duration",
    "match_materials_to_script",
    "video_count",
    "video_source",
    "video_materials",
    "asset_hub_renderer_manifest_path",
    "asset_hub_bundle_uid",
    "asset_hub_scene_mode",
    "asset_hub_strict",
    "custom_audio_file",
    "custom_subtitle_file",
    "subtitle_provider",
    "subtitle_correction_enabled",
    "subtitle_optimization_enabled",
    "video_language",
    "voice_name",
    "voice_volume",
    "voice_rate",
    "bgm_type",
    "bgm_file",
    "bgm_volume",
    "subtitle_enabled",
}


class FakeValidationError(Exception):
    def __init__(self, errors):
        super().__init__("fake validation error")
        self._errors = errors

    def errors(self):
        return self._errors


class FakeVideoParamsV2:
    model_fields = {field: object() for field in VIDEO_PARAMS_TEST_FIELDS}
    calls = []

    @classmethod
    def model_validate(cls, spec):
        cls.calls.append(spec)
        if not spec.get("video_subject"):
            raise FakeValidationError(
                [
                    {
                        "loc": ("video_subject",),
                        "msg": "Field required password=super-secret token:abc123",
                        "type": "missing",
                        "input": "super-secret",
                    }
                ]
            )
        return cls()


class FakeVideoParamsV1:
    __fields__ = {field: object() for field in VIDEO_PARAMS_TEST_FIELDS}
    calls = []

    @classmethod
    def parse_obj(cls, spec):
        cls.calls.append(spec)
        if not spec.get("video_subject"):
            raise FakeValidationError(
                [
                    {
                        "loc": ("video_subject",),
                        "msg": "field required",
                        "type": "value_error.missing",
                    }
                ]
            )
        return cls()


def fake_schema_module(model):
    module = types.ModuleType("app.models.schema")
    module.VideoParams = model
    return module


class TestMptEngineBridge(unittest.TestCase):
    def setUp(self):
        FakeVideoParamsV1.calls = []
        FakeVideoParamsV2.calls = []

    def test_video_params_model_imports_lazily(self):
        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ):
            self.assertIs(get_mpt_video_params_model(), FakeVideoParamsV2)

    def test_capabilities_returns_dict_without_network(self):
        with mock.patch("socket.create_connection") as create_connection:
            capabilities = discover_mpt_engine_capabilities()

        self.assertIsInstance(capabilities, dict)
        self.assertTrue(capabilities["network_free"])
        self.assertIn("pexels", capabilities["sourcing"]["native_video_sources"])
        self.assertIn("pixabay", capabilities["sourcing"]["native_video_sources"])
        self.assertIn("coverr", capabilities["sourcing"]["native_video_sources"])
        create_connection.assert_not_called()

    def test_bridge_generates_generic_mpt_spec(self):
        spec = build_mpt_video_task_from_kurukin_job(
            {
                "job_id": "generic-001",
                "video_subject": "Cafe launch",
                "video_script": "A concise launch script.",
                "stock_source": "pixabay",
                "video_terms": ["coffee shop", "barista"],
                "asset_policy": {"mode": "open_sources"},
                "render_quality": "draft_720p",
            }
        )

        self.assertEqual(spec["kind"], "mpt_video_task_spec")
        self.assertEqual(spec["execution"], "spec_only")
        self.assertEqual(spec["mpt_params"]["video_source"], "pixabay")
        self.assertEqual(spec["mpt_params"]["video_resolution"], "720p")
        self.assertEqual(validate_mpt_task_spec(spec), [])

    def test_generic_bridge_preserves_explicit_zero_bgm_volume(self):
        spec = build_mpt_video_task_from_kurukin_job({"video_subject": "x", "bgm_volume": 0})
        self.assertEqual(spec["mpt_params"]["bgm_volume"], 0.0)

    def test_generic_bridge_parses_string_false_subtitle(self):
        spec = build_mpt_video_task_from_kurukin_job({"video_subject": "x", "subtitle_enabled": "false"})
        self.assertIs(spec["mpt_params"]["subtitle_enabled"], False)

    def test_normalize_mpt_video_params_drops_kurukin_extensions(self):
        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ):
            normalized = normalize_mpt_video_params_spec(
                {
                    "mpt_params": {
                        "video_subject": "Subject",
                        "video_source": "pexels",
                        "render_mode": "aroll_broll",
                        "provider_response": {"secret": "do-not-keep"},
                    }
                }
            )

        self.assertEqual(normalized["video_subject"], "Subject")
        self.assertEqual(normalized["video_source"], "pexels")
        self.assertNotIn("render_mode", normalized)
        self.assertNotIn("provider_response", normalized)

    def test_validate_against_video_params_accepts_generated_spec(self):
        spec = build_mpt_video_task_from_kurukin_job(
            {
                "video_subject": "Cafe launch",
                "video_script": "A concise launch script.",
                "stock_source": "coverr",
                "asset_policy": {"mode": "open_sources"},
            }
        )

        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ):
            result = validate_against_mpt_video_params(spec)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_model"], "FakeVideoParamsV2")
        self.assertEqual(result["errors"], [])
        self.assertEqual(FakeVideoParamsV2.calls[0]["video_source"], "coverr")

    def test_validate_against_video_params_normalizes_and_redacts_errors(self):
        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ):
            result = validate_against_mpt_video_params({"video_subject": ""})

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["field"], "video_subject")
        serialized = repr(result["errors"])
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("abc123", serialized)

    def test_build_validated_mpt_video_task_from_normal_job_is_ok(self):
        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ):
            result = build_validated_mpt_video_task_from_kurukin_job(
                {
                    "job_id": "normal-001",
                    "video_subject": "Cafe launch",
                    "video_script": "A concise launch script.",
                    "video_terms": ["coffee", "barista"],
                    "asset_policy": {"mode": "open_sources"},
                    "stock_source": "pexels",
                }
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_model"], "FakeVideoParamsV2")
        self.assertEqual(result["spec"]["video_source"], "pexels")
        self.assertEqual(result["task_spec"]["execution"], "spec_only")

    def test_validate_against_video_params_supports_pydantic_v1_shape(self):
        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV1)},
        ):
            result = validate_against_mpt_video_params(
                {"video_subject": "Legacy", "video_source": "pixabay"}
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_model"], "FakeVideoParamsV1")
        self.assertEqual(FakeVideoParamsV1.calls[0]["video_source"], "pixabay")

    def test_bridge_preserves_aroll_broll_render_mode_and_policy(self):
        spec = build_mpt_aroll_broll_task_spec(
            {
                "job_id": "aroll-broll-001",
                "render_mode": "aroll_broll",
                "video_subject": "Presenter edit",
                "video_script": "Presenter transcript.",
                "asset_policy": {"mode": "local_only"},
                "a_roll": {
                    "path": "storage/local_videos/presenter.mp4",
                    "audio_policy": "original",
                    "audio_path": "storage/local_audios/presenter.wav",
                },
                "b_roll": {
                    "assets": ["storage/local_videos/cutaway.mp4"],
                    "audio_policy": "muted",
                    "query": "coffee shop b roll",
                },
                "subtitles": {
                    "source": "custom_srt",
                    "custom_srt_path": "storage/local_subtitles/presenter.srt",
                },
            }
        )

        metadata = spec["kurukin_metadata"]
        self.assertEqual(metadata["render_mode"], "aroll_broll")
        self.assertEqual(metadata["asset_policy"]["mode"], "local_only")
        self.assertEqual(
            metadata["aroll_broll"]["primary_media"]["path"],
            "storage/local_videos/presenter.mp4",
        )
        self.assertEqual(
            metadata["aroll_broll"]["support_visuals"]["assets"][0]["url"],
            "storage/local_videos/cutaway.mp4",
        )
        self.assertEqual(
            metadata["aroll_broll"]["original_audio_policy"],
            "a_roll_original",
        )
        self.assertEqual(
            metadata["aroll_broll"]["subtitles_policy"]["custom_srt_path"],
            "storage/local_subtitles/presenter.srt",
        )
        self.assertEqual(
            spec["mpt_params"]["custom_audio_file"],
            "storage/local_audios/presenter.wav",
        )
        self.assertEqual(validate_mpt_task_spec(spec), [])

        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ):
            validation = validate_against_mpt_video_params(spec)

        self.assertTrue(validation["ok"], validation)

    def test_bridge_does_not_call_providers_or_create_pending_task(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            fromlist = args[2] if len(args) > 2 else kwargs.get("fromlist", ())
            if name == "app.services.material" or (
                name == "app.services" and "material" in (fromlist or ())
            ):
                raise AssertionError("provider module must not be imported")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            spec = build_mpt_video_task_from_kurukin_job(
                {
                    "video_subject": "No execution",
                    "stock_source": "pexels",
                    "asset_policy": {"mode": "open_sources"},
                }
            )

        self.assertEqual(spec["execution"], "spec_only")
        self.assertNotIn("pending_path", spec)
        self.assertNotIn("created_task", spec)
        self.assertNotIn("task_id", spec)

    def test_validation_does_not_call_providers_api_or_write_storage(self):
        spec = build_mpt_video_task_from_kurukin_job(
            {
                "video_subject": "No execution",
                "stock_source": "pexels",
                "asset_policy": {"mode": "open_sources"},
            }
        )

        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ), mock.patch("socket.create_connection") as create_connection, mock.patch(
            "pathlib.Path.write_text"
        ) as write_text, mock.patch("builtins.open", mock.mock_open()) as open_file:
            result = validate_against_mpt_video_params(spec)

        self.assertTrue(result["ok"], result)
        create_connection.assert_not_called()
        write_text.assert_not_called()
        open_file.assert_not_called()

    def test_aroll_broll_without_audio_path_documents_gap(self):
        spec = build_mpt_aroll_broll_task_spec(
            {
                "video_subject": "A-roll gap",
                "a_roll": {"path": "storage/local_videos/presenter.mp4"},
                "b_roll": {"assets": ["storage/local_videos/cutaway.mp4"]},
            }
        )

        self.assertTrue(spec["gaps"])
        self.assertIn("custom_audio_file", spec["gaps"][0])
        self.assertEqual(validate_mpt_task_spec(spec), [])

    def test_aroll_broll_local_preserves_metadata_and_documents_gaps(self):
        with mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module(FakeVideoParamsV2)},
        ):
            result = build_validated_mpt_video_task_from_kurukin_job(
                {
                    "video_subject": "A-roll local",
                    "render_mode": "aroll_broll",
                    "a_roll": {"path": "storage/local_videos/presenter.mp4"},
                    "b_roll": {"assets": ["storage/local_videos/cutaway.mp4"]},
                    "asset_policy": {"mode": "local_only"},
                }
            )

        task_spec = result["task_spec"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            task_spec["kurukin_metadata"]["render_mode"],
            "aroll_broll",
        )
        self.assertTrue(task_spec["gaps"])
        self.assertEqual(result["spec"]["video_source"], "local")

    def test_exclusive_brand_assets_do_not_fall_back_to_open_providers(self):
        spec = build_mpt_video_task_from_kurukin_job(
            {
                "video_subject": "Brand campaign",
                "asset_policy": {
                    "mode": "exclusive_brand_assets",
                    "brand_asset_bundle_uid": "bundle-001",
                },
            }
        )

        self.assertEqual(spec["mpt_params"]["video_source"], "local")
        self.assertEqual(spec["mpt_params"]["video_terms"], [])
        self.assertTrue(spec["gaps"])

    def test_validate_detects_missing_fields(self):
        spec = {
            "kind": "mpt_video_task_spec",
            "execution": "spec_only",
            "safe_to_build_without_side_effects": True,
            "mpt_params": {"video_source": "local", "video_subject": ""},
            "kurukin_metadata": {
                "render_mode": "normal",
                "asset_policy": {"mode": "local_only"},
            },
        }

        errors = validate_mpt_task_spec(spec)

        self.assertIn("mpt_params.video_subject or video_script is required", errors)
        self.assertIn(
            "mpt_params.video_materials is required when video_source is local",
            errors,
        )

    def test_summary_is_human_readable(self):
        spec = build_mpt_video_task_from_kurukin_job(
            {
                "video_subject": "Summary",
                "video_materials": [
                    {"provider": "local", "url": "storage/local_videos/one.mp4"}
                ],
                "asset_policy": {"mode": "local_only"},
                "custom_audio_file": "storage/local_audios/audio.mp3",
            }
        )

        summary = summarize_mpt_task_spec(spec)

        self.assertTrue(summary["valid"], summary)
        self.assertEqual(summary["engine"], "moneyprinterturbo")
        self.assertEqual(summary["video_source"], "local")
        self.assertEqual(summary["material_count"], 1)
        self.assertTrue(summary["custom_audio"])


if __name__ == "__main__":
    unittest.main()
