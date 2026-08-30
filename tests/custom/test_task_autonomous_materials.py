import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if importlib.util.find_spec("openai") is None:
    raise unittest.SkipTest("task service optional dependencies are not installed")

from app.models.schema import MaterialInfo, VideoParams
from app.services import task
from app.custom.material_discovery import MaterialDiscoveryResult


class TestAutonomousMaterialPreparation(unittest.TestCase):
    def policy(self):
        return {"providers": {"enabled": ["pexels"]}}

    def title_policy(self):
        return {
            "providers": {"enabled": ["asset_hub"]},
            "asset_hub": {"include": {"titles": ["mi-otra-yo"]}},
        }

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
