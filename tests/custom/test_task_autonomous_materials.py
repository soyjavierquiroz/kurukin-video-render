import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _install_openai_import_stub() -> None:
    """Allow task's pure selection seam to load without the optional SDK.

    The selected task tests patch every LLM-facing operation.  This test-only
    package shape satisfies the imports made by ``llm`` and ``voice`` without
    adding a production fallback or simulating any network client behavior.
    """
    if importlib.util.find_spec("openai") is not None:
        return

    openai = types.ModuleType("openai")
    openai.__path__ = []
    openai.OpenAI = type("OpenAI", (), {})
    openai.AzureOpenAI = type("AzureOpenAI", (), {})
    openai_types = types.ModuleType("openai.types")
    openai_types.__path__ = []
    openai_chat = types.ModuleType("openai.types.chat")
    openai_chat.ChatCompletion = type("ChatCompletion", (), {})
    openai.types = openai_types
    openai_types.chat = openai_chat
    sys.modules.update({
        "openai": openai,
        "openai.types": openai_types,
        "openai.types.chat": openai_chat,
    })


def _install_edge_tts_import_stub() -> None:
    """Satisfy voice's import-time annotations; no TTS path is exercised."""
    if importlib.util.find_spec("edge_tts") is not None:
        return

    edge_tts = types.ModuleType("edge_tts")
    edge_tts.Communicate = type("Communicate", (), {})
    edge_tts.SubMaker = type("SubMaker", (), {})
    sys.modules["edge_tts"] = edge_tts


_install_openai_import_stub()
_install_edge_tts_import_stub()

from app.models.schema import MaterialInfo, VideoParams
from app.custom.atlas_runtime import AtlasRuntimeConfigurationError
from app.custom.video_terms import normalize_video_terms
from app.services import task
from app.custom.material_discovery import MaterialDiscoveryResult


class TestAutonomousMaterialPreparation(unittest.TestCase):
    def policy(self):
        return {"providers": {"enabled": ["pexels"]}}

    def atlas_policy(self):
        return {"providers": {"enabled": ["atlas"]}}

    def title_policy(self):
        return {
            "providers": {"enabled": ["asset_hub"]},
            "asset_hub": {"include": {"titles": ["mi-otra-yo"]}},
        }

    def test_video_terms_omitted_or_blank_use_native_generated_fallback(self):
        for supplied, params in (
            ("omitted", VideoParams(video_subject="cat", match_materials_to_script=True)),
            ("blank", VideoParams(video_subject="cat", video_terms="", match_materials_to_script=True)),
        ):
            with self.subTest(supplied=supplied):
                with patch.object(task.llm, "generate_terms", return_value=["generated cat"]) as generate:
                    self.assertEqual(task.generate_terms("t1", params, "script"), ["generated cat"])
                generate.assert_called_once()

    def test_normalize_video_terms_uses_native_comma_contract_exactly(self):
        self.assertIs(task.normalize_video_terms, normalize_video_terms)
        self.assertEqual(normalize_video_terms(" café ,barista，night shift "), ["café", "barista", "night shift"])

    def test_operator_terms_seed_stock_discovery_for_human_review(self):
        params = SimpleNamespace(
            material_source_policy=self.policy(), asset_hub_terms=[], video_aspect="9:16",
            video_clip_duration=5, human_review={"video_terms_source": "operator"}, editorial_profile={},
        )
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        terms = ["coffee shop", "barista hands"]
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("t1", params, terms, 10, "a script that must not replace terms")
        self.assertEqual(discover.call_args.kwargs["stock_terms"], terms)

    def test_generated_terms_seed_scene_driven_provider_queries_for_human_review(self):
        params = SimpleNamespace(
            material_source_policy=self.policy(), asset_hub_terms=[], video_aspect="9:16",
            video_clip_duration=5, human_review={"video_terms_source": "generated"}, editorial_profile={},
        )
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("t1", params, ["worried person"], 10, "Una persona agotada descansa en casa")
        stock_terms = discover.call_args.kwargs["stock_terms"]
        self.assertIn("worried person", stock_terms)
        self.assertTrue(any("tired" in term or "home" in term for term in stock_terms))

    def test_human_review_plan_records_operator_video_terms_provenance(self):
        params = SimpleNamespace(
            material_source_policy=self.policy(), video_aspect="9:16", editorial_profile={},
            human_review={"video_terms_source": "operator", "video_terms_raw": "café, barista"},
        )
        selection = SimpleNamespace(decisions=(), target_count=0)
        plan = {"segments": []}
        with patch.object(task, "_select_autonomous_materials", return_value=(SimpleNamespace(), selection)), \
             patch.object(task, "build_discovery_plan", return_value={}), \
             patch.object(task.human_review, "build_plan", return_value=plan), \
             patch.object(task.sm.state, "update_task"):
            task._prepare_human_review_plan("t1", params, "script", ["café", "barista"], "audio.mp3", 10)
        self.assertEqual(plan["video_terms_source"], "operator")
        self.assertEqual(plan["video_terms_raw"], "café, barista")
        self.assertEqual(plan["video_terms_resolved"], ["café", "barista"])

    def test_policy_runs_discover_select_acquire_and_uses_audio_duration(self):
        params = VideoParams(video_subject="cat", material_source_policy=self.policy(), asset_hub_terms=["hub cat"])
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        acquired = [MaterialInfo(provider="pexels", url="/tmp/cat.mp4")]
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, patch.object(task, "select_material_candidates", return_value=selection) as select, patch.object(task, "acquire_selected_materials", return_value=SimpleNamespace(materials=acquired)) as acquire, patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            error = task._prepare_autonomous_materials("t1", params, ["stock cat"], 17)
        self.assertIsNone(error)
        self.assertEqual(params.video_source, "local")
        self.assertEqual(params.video_materials, acquired)
        self.assertEqual(discover.call_args.kwargs["asset_hub_terms"], ["hub cat"])
        self.assertEqual(select.call_args.kwargs["target_duration"], 17)
        acquire.assert_called_once_with(selection_result=selection, task_id="t1")

    def test_non_atlas_policy_does_not_construct_or_validate_atlas_runtime(self):
        params = VideoParams(video_subject="cat", material_source_policy=self.policy())
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        acquired = [MaterialInfo(provider="pexels", url="/tmp/cat.mp4")]
        with patch.dict(task.os.environ, {"ATLAS_ENABLED": "not-a-boolean"}, clear=False), \
             patch.object(task, "build_atlas_runtime_from_env") as build_runtime, \
             patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task, "acquire_selected_materials", return_value=SimpleNamespace(materials=acquired)) as acquire, \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            self.assertIsNone(task._prepare_autonomous_materials("t1", params, ["cat"], 5))

        build_runtime.assert_not_called()
        self.assertIsNone(discover.call_args.kwargs["atlas_provider"])
        acquire.assert_called_once_with(selection_result=selection, task_id="t1")

    def test_atlas_runtime_is_built_once_and_injected_through_both_stages(self):
        params = VideoParams(
            video_subject="display title",
            atlas_title_id="romper-el-circulo",
            material_source_policy=self.atlas_policy(),
        )
        shared_client = object()
        runtime = SimpleNamespace(
            client=shared_client,
            provider=SimpleNamespace(client=shared_client),
            materializer=SimpleNamespace(client=shared_client),
        )
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        acquired = [MaterialInfo(provider="atlas", url="/tmp/atlas.mp4")]
        with patch.object(task, "build_atlas_runtime_from_env", return_value=runtime) as build_runtime, \
             patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task, "acquire_selected_materials", return_value=SimpleNamespace(materials=acquired)) as acquire, \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            self.assertIsNone(task._prepare_autonomous_materials("t1", params, ["scene"], 5))

        build_runtime.assert_called_once_with()
        self.assertIs(discover.call_args.kwargs["atlas_provider"], runtime.provider)
        self.assertEqual(
            discover.call_args.kwargs["atlas_scope"],
            {"kind": "title", "title_id": "romper-el-circulo"},
        )
        acquire.assert_called_once_with(
            selection_result=selection,
            task_id="t1",
            atlas_materializer=runtime.materializer,
        )
        self.assertIs(runtime.provider.client, runtime.client)
        self.assertIs(runtime.materializer.client, runtime.client)

    def test_disabled_atlas_runtime_keeps_discovery_config_missing_authoritative(self):
        params = VideoParams(
            video_subject="display title",
            atlas_title_id="romper-el-circulo",
            material_source_policy=self.atlas_policy(),
        )
        discovery = SimpleNamespace(candidates=())
        selection = SimpleNamespace(decisions=(), shortfall=0, selected_count=0)
        with patch.object(task, "build_atlas_runtime_from_env", return_value=None) as build_runtime, \
             patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task, "acquire_selected_materials") as acquire, \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            self.assertEqual(
                task._prepare_autonomous_materials("t1", params, ["scene"], 5),
                "No usable visual materials found",
            )

        build_runtime.assert_called_once_with()
        self.assertIsNone(discover.call_args.kwargs["atlas_provider"])
        acquire.assert_not_called()

    def test_invalid_atlas_runtime_configuration_surfaces_without_fallback(self):
        params = VideoParams(video_subject="display title", material_source_policy=self.atlas_policy())
        configuration_error = AtlasRuntimeConfigurationError("ATLAS_BASE_URL is required")
        with patch.object(task, "build_atlas_runtime_from_env", side_effect=configuration_error), \
             patch.object(task, "discover_material_candidates") as discover, \
             patch.object(task, "acquire_selected_materials") as acquire:
            with self.assertRaisesRegex(AtlasRuntimeConfigurationError, "ATLAS_BASE_URL"):
                task._prepare_autonomous_materials("t1", params, ["scene"], 5)

        discover.assert_not_called()
        acquire.assert_not_called()

    def test_runtime_does_not_broaden_missing_atlas_title_identity(self):
        params = VideoParams(video_subject="display title", material_source_policy=self.atlas_policy())
        runtime = SimpleNamespace(provider=object(), materializer=object())
        discovery = SimpleNamespace(candidates=())
        selection = SimpleNamespace(decisions=(), shortfall=0, selected_count=0)
        with patch.object(task, "build_atlas_runtime_from_env", return_value=runtime), \
             patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            self.assertEqual(
                task._prepare_autonomous_materials("t1", params, ["scene"], 5),
                "No usable visual materials found",
            )

        self.assertIs(discover.call_args.kwargs["atlas_provider"], runtime.provider)
        self.assertIsNone(discover.call_args.kwargs["atlas_scope"])

    def test_explicit_atlas_identity_reaches_discovery_as_a_title_only_scope(self):
        params = VideoParams(
            video_subject="A display subject must not be used as scope",
            atlas_title_id="romper-el-circulo",
            material_source_policy=self.atlas_policy(),
        )
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("unrelated-task-id", params, ["term"], 5)

        self.assertEqual(
            discover.call_args.kwargs["atlas_scope"],
            {"kind": "title", "title_id": "romper-el-circulo"},
        )

    def test_video_subject_never_becomes_an_atlas_scope(self):
        params = VideoParams(
            video_subject="romper-el-circulo",
            material_source_policy=self.atlas_policy(),
        )
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("unrelated-task-id", params, ["term"], 5)

        self.assertIsNone(discover.call_args.kwargs["atlas_scope"])

    def test_asset_hub_scope_never_becomes_an_atlas_scope(self):
        params = VideoParams(
            video_subject="display title",
            asset_hub_bundle_uid="romper-el-circulo",
            material_source_policy={
                "providers": {"enabled": ["atlas", "asset_hub"]},
                "asset_hub": {"include": {"titles": ["romper-el-circulo"]}},
            },
        )
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("unrelated-task-id", params, ["term"], 5)

        self.assertIsNone(discover.call_args.kwargs["atlas_scope"])

    def test_task_id_never_becomes_an_atlas_scope(self):
        params = VideoParams(video_subject="display title", material_source_policy=self.atlas_policy())
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("romper-el-circulo", params, ["term"], 5)

        self.assertIsNone(discover.call_args.kwargs["atlas_scope"])

    def test_missing_identity_never_becomes_a_broad_atlas_scope(self):
        params = VideoParams(video_subject="display title", material_source_policy=self.atlas_policy())
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("unrelated-task-id", params, ["term"], 5)

        self.assertIsNone(discover.call_args.kwargs["atlas_scope"])

    def test_non_atlas_policy_discards_even_an_explicit_atlas_identity(self):
        params = VideoParams(
            video_subject="existing job",
            atlas_title_id="romper-el-circulo",
            material_source_policy=self.policy(),
        )
        discovery = SimpleNamespace(candidates=(SimpleNamespace(),))
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=0, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=discovery) as discover, \
             patch.object(task, "select_material_candidates", return_value=selection), \
             patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            task._select_autonomous_materials("t1", params, ["term"], 5)

        self.assertIsNone(discover.call_args.kwargs["atlas_scope"])

    def test_title_only_global_fallback_runs_once_on_shortfall(self):
        params = VideoParams(video_subject="cat", material_source_policy=self.title_policy())
        discovery = SimpleNamespace(candidates=(SimpleNamespace(dedupe_key="real"),))
        fallback = SimpleNamespace(candidates=(SimpleNamespace(dedupe_key="title"),), diagnostics=(), providers_attempted=(), providers_succeeded=())
        first_selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=1, selected_count=1)
        final_selection = SimpleNamespace(decisions=(SimpleNamespace(), SimpleNamespace()), shortfall=0, selected_count=2)
        acquired = [MaterialInfo(provider="asset_hub", url="/tmp/one.mp4")]

        with patch.object(task, "discover_material_candidates", return_value=discovery), patch.object(task, "discover_asset_hub_title_fallback_candidates", return_value=fallback) as title_fallback, patch.object(task, "select_material_candidates", side_effect=(first_selection, final_selection)) as select, patch.object(task, "acquire_selected_materials", return_value=SimpleNamespace(materials=acquired)), patch.object(task.material, "recent_external_asset_keys", return_value=set()):
            error = task._prepare_autonomous_materials("t1", params, ["pareja discutiendo", "niño solo"], 10)

        self.assertIsNone(error)
        title_fallback.assert_called_once()
        self.assertEqual(select.call_count, 2)

    def test_shortfall_warns_and_continues(self):
        params = VideoParams(video_subject="cat", material_source_policy=self.policy())
        selection = SimpleNamespace(decisions=(SimpleNamespace(),), shortfall=2, selected_count=1)
        with patch.object(task, "discover_material_candidates", return_value=SimpleNamespace(candidates=(SimpleNamespace(),))), patch.object(task, "select_material_candidates", return_value=selection), patch.object(task, "acquire_selected_materials", return_value=SimpleNamespace(materials=[])), patch.object(task.material, "recent_external_asset_keys", return_value=set()), patch.object(task.logger, "warning") as warning:
            self.assertIsNone(task._prepare_autonomous_materials("t1", params, ["cat"], 5))
        self.assertTrue(warning.called)

    def test_no_candidates_has_clear_error(self):
        params = VideoParams(video_subject="cat", material_source_policy=self.policy())
        with patch.object(task, "discover_material_candidates", return_value=SimpleNamespace(candidates=())):
            self.assertEqual(task._prepare_autonomous_materials("t1", params, ["cat"], 5), "No usable visual materials found")

    def test_human_review_skips_v1_and_derives_target_count_from_audio(self):
        params = SimpleNamespace(
            material_source_policy={
                "providers": {"enabled": ["asset_hub"]},
                "asset_hub": {"include": {"generic": True}},
            },
            asset_hub_terms=["tema"], video_aspect="9:16", video_clip_duration=5,
            human_review={"enabled": True}, editorial_profile={},
        )
        reserve = MaterialDiscoveryResult((), (), ("asset_hub",), (), {"stock": (), "asset_hub": ("consulta",)})
        with patch.object(task, "discover_material_candidates") as v1_discover, \
             patch.object(task, "select_material_candidates") as v1_select, \
             patch.object(task.human_review, "visual_queries_for_review_segments", return_value=[("consulta",)] * 3), \
             patch.object(task, "discover_asset_hub_review_reserve_candidates", return_value=reserve) as v2_reserve:
            discovery, selection = task._select_autonomous_materials("t1", params, ["tema"], 12, "guion")
        v1_discover.assert_not_called()
        v1_select.assert_not_called()
        v2_reserve.assert_called_once()
        self.assertEqual(selection.target_count, 3)
        self.assertEqual(selection.decisions, ())
        self.assertEqual(discovery.candidates, ())

    def test_pipeline_skips_discovery_without_policy_and_with_explicit_manifest(self):
        for policy, manifest in ((None, ""), (self.policy(), "/explicit.json")):
            with self.subTest(policy=policy, manifest=manifest):
                params = VideoParams(video_subject="cat", material_source_policy=policy, asset_hub_renderer_manifest_path=manifest)
                with patch.object(task, "generate_script", return_value="script"), patch.object(task, "apply_asset_hub_renderer_manifest"), patch.object(task, "generate_terms", return_value=["cat"]), patch.object(task, "save_script_data"), patch.object(task, "generate_audio", return_value=("audio.mp3", 5, None)), patch.object(task, "generate_subtitle", return_value=""), patch.object(task, "get_video_materials", return_value=["cat.mp4"]), patch.object(task, "discover_material_candidates") as discover, patch.object(task.sm.state, "update_task"):
                    result = task._run_pipeline("t1", params, stop_at="materials")
                discover.assert_not_called()
                self.assertEqual(result["materials"], ["cat.mp4"])


if __name__ == "__main__":
    unittest.main()
