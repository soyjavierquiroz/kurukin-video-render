import json
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from app.custom import human_review
from app.custom.material_discovery import DiscoveryDiagnostic, MaterialCandidate, MaterialDiscoveryResult
from app.custom.material_selection import MaterialSelectionDecision, MaterialSelectionOptions, MaterialSelectionResult
from app.models.schema import VideoParams
from app.models.schema import MaterialInfo
from scripts import batch_mpt_worker
from scripts import nightly_runner
from scripts import nightly_preflight
from scripts import produce_batch

def _install_task_import_stubs() -> None:
    """Stub services unrelated to the human-review task path.

    ``task`` imports the LLM and TTS services eagerly, but these pipeline tests
    mock those collaborators and never execute them.  The host test environment
    intentionally does not install their optional SDKs (``openai``/``edge_tts``).
    """
    sys.modules.setdefault("app.services.llm", ModuleType("app.services.llm"))

    voice_module = ModuleType("app.services.voice")
    voice_module.create_subtitle = lambda *args, **kwargs: None
    voice_module.get_audio_duration = lambda *args, **kwargs: 1
    voice_module.parse_voice_name = lambda value: value
    voice_module.tts = lambda *args, **kwargs: object()
    sys.modules.setdefault("app.services.voice", voice_module)


_install_task_import_stubs()
from app.services import task


def candidate(uid, term="term", provider="pexels", url=None, source_info=None, duration=5, width=1080, height=1920, orientation="portrait"):
    return MaterialCandidate(
        provider=provider,
        canonical_id=uid,
        dedupe_key=uid,
        search_term=term,
        rank=1,
        url=url if url is not None else f"https://example.test/{uid}.mp4",
        duration=duration,
        width=width,
        height=height,
        orientation=orientation,
        source_info=source_info or {},
    )


class FakeImageResponse:
    headers = {"content-type": "image/jpeg"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield b"jpeg-bytes"


class FakeHttpResponse(FakeImageResponse):
    def __init__(self, status_code=200, content_type="image/jpeg", chunks=(b"jpeg-bytes",)):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        yield from self._chunks


def decision(item):
    return MaterialSelectionDecision(item, 40, 20, 10, 20, 15, 3, 108, 5)


def selection(selected):
    return MaterialSelectionResult(
        MaterialSelectionOptions("9:16", 5, 5),
        tuple(decision(item) for item in selected),
        1,
        len(selected),
        0,
        False,
        ("term",),
        5,
    )


def load_review_app_module():
    module_name = "review_app_for_human_review_tests"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path("scripts/review_app.py"),
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"streamlit": SimpleNamespace(), module_name: module}):
        spec.loader.exec_module(module)
    return module


class TestHumanReviewPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_plan_pending_with_selected_segments_and_three_alternatives(self):
        selected = candidate("asset-1")
        alternatives = [candidate(f"asset-{index}") for index in range(2, 7)]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([selected]),
            discovery_result=SimpleNamespace(candidates=tuple([selected] + alternatives)),
            output_path=plan_file,
        )

        self.assertEqual(plan["schema_version"], human_review.SCHEMA_VERSION)
        self.assertEqual(plan["review_status"], human_review.STATUS_PENDING)
        self.assertEqual(plan["segments"][0]["selected_asset"]["asset_uid"], "asset-1")
        self.assertEqual(len(plan["segments"][0]["alternatives"]), 3)
        self.assertTrue(Path(plan["segments"][0]["selected_asset"]["thumbnail_path"]).exists())
        self.assertTrue(plan["segments"][0]["selected_asset"]["flip_horizontal"])

    def test_extended_asset_hub_metadata_survives_primary_suggested_and_backup(self):
        metadata = {
            "duration": 7.1, "width": 1080, "height": 1350, "orientation": "vertical-4x5",
            "primary_theme": "vulnerabilidad", "primary_topic": "aceptar ayuda",
            "visual_description": "Una mujer sentada mirando hacia un lado.",
            "action_description": "Permanece quieta con expresión preocupada.",
            "contains_people": True, "people_count": 1, "visual_presentation": "feminine",
            "visual_presentation_confidence": 0.98, "person_visibility": "clear",
        }
        selected = candidate("asset-primary", provider="asset_hub", source_info=metadata, duration=7.1, width=1080, height=1350, orientation="vertical-4x5")
        suggested = candidate("asset-suggested", provider="asset_hub", source_info=metadata, duration=7.1, width=1080, height=1350, orientation="vertical-4x5")
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch", task_id="task-1", stem="story", audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt", script_text="script", duration=5, aspect_ratio="9:16",
            visual_style="none", selection_result=selection([selected]),
            discovery_result=SimpleNamespace(candidates=(selected, suggested)), output_path=plan_file,
        )
        human_review.set_segment_backup(plan_file, "segment-001", "asset-suggested", True)
        persisted = human_review.read_json(plan_file)["segments"][0]

        for asset in (persisted["selected_asset"], persisted["alternatives"][0], persisted["backup_assets"][0]):
            self.assertEqual(asset["metadata"], metadata)

    def test_v2_ineligible_asset_hub_candidate_is_not_selected_or_suggested(self):
        rejected = candidate("still-image", provider="asset_hub", source_info={"media_type": "image"})
        valid = candidate("valid-video", provider="asset_hub", source_info={})
        plan = human_review.build_plan(
            batch_id="batch", task_id="task-1", stem="story", audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt", script_text="script", duration=5, aspect_ratio="9:16",
            visual_style="none", selection_result=selection([rejected]),
            discovery_result=SimpleNamespace(candidates=(rejected, valid)),
            output_path=self.root / "production-plan.json",
        )
        segment = plan["segments"][0]
        visible_uids = [segment["selected_asset"]["asset_uid"]] + [item["asset_uid"] for item in segment["alternatives"]]
        self.assertEqual(segment["selected_asset"]["asset_uid"], "valid-video")
        self.assertNotIn("still-image", visible_uids)

    def test_v2_minimal_asset_hub_candidate_remains_selectable(self):
        minimal = candidate("minimal-video", provider="asset_hub", source_info={}, duration=None, width=None, height=None, orientation=None)
        plan = human_review.build_plan(
            batch_id="batch", task_id="task-1", stem="story", audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt", script_text="script", duration=5, aspect_ratio="9:16",
            visual_style="none", selection_result=selection([minimal]),
            discovery_result=SimpleNamespace(candidates=(minimal,)),
            output_path=self.root / "minimal-plan.json",
        )
        self.assertEqual(plan["segments"][0]["selected_asset"]["asset_uid"], "minimal-video")

    def test_build_plan_preserves_material_scope_policy(self):
        selected = candidate("asset-1", provider="asset_hub", source_info={"title": "mi-otra-yo"})
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            material_source_policy={"providers": {"enabled": ["asset_hub"]}},
            asset_hub_source_policy={"sources": [{"scope": "title", "title": "mi-otra-yo"}]},
            material_title="mi-otra-yo",
            source_policy="title-exclusive",
            selection_result=selection([selected]),
            discovery_result=SimpleNamespace(candidates=(selected,)),
            output_path=plan_file,
        )

        self.assertEqual(plan["material_title"], "mi-otra-yo")
        self.assertEqual(plan["title_scope"], "mi-otra-yo")
        self.assertEqual(plan["source_policy"], "title-exclusive")
        self.assertEqual(plan["asset_hub_source_policy"], {"sources": [{"scope": "title", "title": "mi-otra-yo"}]})

    def test_selected_assets_prefer_unused_candidates_across_segments(self):
        assets = [candidate(uid) for uid in ("asset-a", "asset-b", "asset-c")]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Uno. Dos. Tres.",
            duration=15,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([assets[0], assets[0], assets[0]]),
            discovery_result=SimpleNamespace(candidates=tuple(assets)),
            output_path=plan_file,
        )

        self.assertEqual(
            [segment["selected_asset"]["asset_uid"] for segment in plan["segments"]],
            ["asset-a", "asset-b", "asset-c"],
        )

    def test_second_segment_uses_next_ranked_candidate_when_first_was_used(self):
        asset_a = candidate("asset-a")
        asset_b = candidate("asset-b")
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Uno. Dos.",
            duration=10,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([asset_a, asset_a]),
            discovery_result=SimpleNamespace(candidates=(asset_a, asset_b)),
            output_path=plan_file,
        )

        self.assertEqual(
            [segment["selected_asset"]["asset_uid"] for segment in plan["segments"]],
            ["asset-a", "asset-b"],
        )

    def test_primary_allocation_prefers_previewable_without_reordering_editorial_candidates(self):
        positive_previewable = candidate("positive-previewable")
        unknown_previewable = candidate("unknown-previewable")
        non_previewable = candidate("non-previewable")
        chosen, repeated = human_review._select_segment_candidate(
            [positive_previewable, non_previewable, unknown_previewable], set(), set(),
            is_review_previewable=lambda item: item.canonical_id != "non-previewable",
        )
        self.assertEqual(chosen.canonical_id, "positive-previewable")
        self.assertFalse(repeated)

    def test_primary_allocation_does_not_use_previewable_mismatch_over_valid_candidate(self):
        valid_non_previewable = candidate("valid-non-previewable")
        mismatch_previewable = candidate("mismatch-previewable")
        chosen, _repeated = human_review._select_segment_candidate(
            [mismatch_previewable, valid_non_previewable], set(), set(),
            is_review_previewable=lambda item: item.canonical_id == "mismatch-previewable",
            is_primary_eligible=lambda item: item.canonical_id != "mismatch-previewable",
        )
        self.assertEqual(chosen.canonical_id, "valid-non-previewable")

    def test_primary_allocation_uses_next_previewable_candidate_after_global_use(self):
        first = candidate("first-previewable")
        second = candidate("second-previewable")
        chosen, _repeated = human_review._select_segment_candidate(
            [first, second], {"first-previewable"}, set(),
            is_review_previewable=lambda _item: True,
        )
        self.assertEqual(chosen.canonical_id, "second-previewable")

    def test_build_plan_persists_previewability_and_promotes_previewable_primary(self):
        selected_non_previewable = candidate("selected-non-previewable")
        inspectable = candidate("inspectable")

        def preview(candidate_item, _thumbnails_dir):
            if candidate_item.canonical_id == "inspectable":
                return {"status": "available", "type": "url", "value": "https://example.test/inspectable.jpg"}, []
            return {"status": "unavailable", "type": "none", "value": ""}, []

        with patch("app.custom.human_review.ensure_candidate_preview", side_effect=preview):
            plan = human_review.build_plan(
                batch_id="batch", task_id="task", stem="story", audio_path="/tmp/audio.mp3",
                script_path="/tmp/story.txt", script_text="Escena simple.", duration=5,
                aspect_ratio="9:16", visual_style="none", selection_result=selection([selected_non_previewable]),
                discovery_result=SimpleNamespace(candidates=(selected_non_previewable, inspectable)),
                output_path=self.root / "inspectable-primary.json",
            )
        primary = plan["segments"][0]["selected_asset"]
        self.assertEqual(primary["asset_uid"], "inspectable")
        self.assertTrue(primary["review_previewable"])

    def test_used_positive_primary_skips_to_next_positive_before_unknown(self):
        positive_a = candidate(
            "positive-a", source_info={"visual_description": "worried woman resting alone at home"},
        )
        positive_b = candidate(
            "positive-b", source_info={"visual_description": "tired woman resting quietly at home"},
        )
        unknown = candidate(
            "unknown", source_info={"editorial_quality": 100, "contains_people": True},
        )
        plan = human_review.build_plan(
            batch_id="batch", task_id="task-1", stem="story", audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt", script_text="Descansa con culpa. Descansa con culpa.",
            duration=10, aspect_ratio="9:16", visual_style="none",
            selection_result=selection([positive_a, positive_a]),
            discovery_result=SimpleNamespace(candidates=(positive_a, positive_b, unknown)),
            output_path=self.root / "positive-allocation.json",
        )
        self.assertEqual(
            [segment["selected_asset"]["asset_uid"] for segment in plan["segments"]],
            ["positive-a", "positive-b"],
        )

    def test_previewable_alternatives_fill_visible_slots_before_unavailable_candidates(self):
        selected = candidate("selected", source_info={
            "visual_description": "worried woman resting alone at home",
            "thumbnail_url": "https://img.example/selected.jpg",
        })
        unavailable = candidate("unavailable")
        previewable = [
            candidate(f"previewable-{index}", source_info={"thumbnail_url": f"https://img.example/{index}.jpg"})
            for index in range(1, 4)
        ]
        with patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()), \
             patch("app.custom.human_review.subprocess.run"):
            plan = human_review.build_plan(
                batch_id="batch", task_id="task", stem="story", audio_path="/tmp/audio.mp3",
                script_path="/tmp/story.txt", script_text="Escena simple.", duration=5,
                aspect_ratio="9:16", visual_style="none", selection_result=selection([selected]),
                discovery_result=SimpleNamespace(candidates=tuple([selected, unavailable, *previewable])),
                output_path=self.root / "previewable-slots.json",
            )

        alternatives = plan["segments"][0]["alternatives"]
        self.assertEqual([item["asset_uid"] for item in alternatives], [item.canonical_id for item in previewable])
        self.assertTrue(all(human_review.review_previewable(item["preview"]) for item in alternatives))

    def test_unavailable_scarcity_alternative_is_retained_but_not_review_previewable(self):
        selected = candidate("selected")
        unavailable = candidate("unavailable")
        with patch("app.custom.human_review.subprocess.run"):
            plan = human_review.build_plan(
                batch_id="batch", task_id="task", stem="story", audio_path="/tmp/audio.mp3",
                script_path="/tmp/story.txt", script_text="Escena simple.", duration=5,
                aspect_ratio="9:16", visual_style="none", selection_result=selection([selected]),
                discovery_result=SimpleNamespace(candidates=(selected, unavailable)),
                output_path=self.root / "preview-scarcity.json",
            )

        alternative = plan["segments"][0]["alternatives"][0]
        self.assertEqual(alternative["asset_uid"], "unavailable")
        self.assertFalse(human_review.review_previewable(alternative["preview"]))
        self.assertFalse(alternative["review_previewable"])
        self.assertTrue(alternative["diagnostic_only"])
        self.assertEqual(plan["segments"][0]["warnings"][0]["code"], "preview_unavailable")

    def test_visible_alternatives_keep_v2_positive_editorial_order(self):
        selected = candidate("selected", source_info={"visual_description": "worried woman resting alone at home"})
        unknown = candidate("asset-unknown", source_info={"editorial_quality": 100, "contains_people": True})
        positive = candidate("positive", source_info={"visual_description": "worried woman resting alone at home"})
        with patch("app.custom.human_review.subprocess.run"):
            plan = human_review.build_plan(
                batch_id="batch", task_id="task", stem="story", audio_path="/tmp/audio.mp3",
                script_path="/tmp/story.txt", script_text="Una mujer siente culpa al descansar en casa.", duration=5,
                aspect_ratio="9:16", visual_style="none", selection_result=selection([selected]),
                discovery_result=SimpleNamespace(candidates=(selected, unknown, positive)),
                output_path=self.root / "editorial-order.json",
            )

        self.assertEqual([item["asset_uid"] for item in plan["segments"][0]["alternatives"]], ["positive", "asset-unknown"])

    def test_scene_queries_prefer_scene_derived_asset_over_old_hint(self):
        old_hint = candidate("old-hint", term="niña sola")
        scene_asset = candidate("scene-asset", term="persona culpa agotamiento")
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Por eso descansar te da culpa.",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([old_hint]),
            discovery_result=SimpleNamespace(candidates=(old_hint, scene_asset)),
            output_path=plan_file,
        )

        segment = plan["segments"][0]
        self.assertEqual(segment["selected_asset"]["asset_uid"], "scene-asset")
        self.assertIn("persona culpa agotamiento", segment["search_terms"])
        self.assertNotEqual(segment["search_terms"], ["niña sola"])

    def test_feminine_editorial_profile_boosts_without_hard_filtering(self):
        masculine = candidate(
            "asset-man",
            term="hombre tristeza",
            source_info={"filename": "hombre_triste.mp4"},
        )
        feminine = candidate(
            "asset-woman",
            term="mujer tristeza",
            source_info={"filename": "mujer_triste.mp4"},
        )
        girl = candidate(
            "asset-girl",
            term="niña vulnerable",
            source_info={"filename": "nina_vulnerable.mp4"},
        )
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Ella necesitaba construir una vida propia.",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            editorial_profile={"subject_gender": "feminine"},
            selection_result=selection([masculine]),
            discovery_result=SimpleNamespace(candidates=(masculine, feminine, girl)),
            output_path=plan_file,
        )

        segment = plan["segments"][0]
        visible_uids = [segment["selected_asset"]["asset_uid"]] + [
            item["asset_uid"] for item in segment["alternatives"]
        ]
        self.assertEqual(plan["editorial_profile"], {"subject_gender": "feminine"})
        self.assertEqual(segment["selected_asset"]["asset_uid"], "asset-woman")
        self.assertNotIn("asset-man", visible_uids)
        self.assertTrue(any("mujer" in term or "niña" in term for term in segment["search_terms"]))

    def test_feminine_strict_visible_candidates(self):
        masculine = candidate("asset-man", term="hombre triste", source_info={"title": "hombre triste"})
        boy = candidate("asset-boy", term="niño solo", source_info={"title": "niño solo"})
        feminine = candidate("asset-woman", term="mujer triste", source_info={"title": "mujer triste"})
        girl = candidate("asset-girl", term="niña sola", source_info={"title": "niña sola"})
        unknown = candidate("asset-unknown", term="persona triste", source_info={"title": "persona triste"})
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Ella estaba triste.",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            editorial_profile={"subject_gender": "feminine"},
            selection_result=selection([masculine]),
            discovery_result=SimpleNamespace(candidates=(masculine, boy, feminine, girl, unknown)),
            output_path=plan_file,
        )

        visible_uids = [plan["segments"][0]["selected_asset"]["asset_uid"]] + [
            item["asset_uid"] for item in plan["segments"][0]["alternatives"]
        ]
        self.assertIn("asset-woman", visible_uids)
        self.assertIn("asset-girl", visible_uids)
        self.assertNotIn("asset-man", visible_uids)
        self.assertNotIn("asset-boy", visible_uids)
        self.assertNotIn("asset-unknown", visible_uids)

    def test_neutral_does_not_filter_and_mixed_allows_both(self):
        masculine = candidate("asset-man", term="hombre triste", source_info={"title": "hombre triste"})
        feminine = candidate("asset-woman", term="mujer triste", source_info={"title": "mujer triste"})
        for subject_gender in ("neutral", "mixed"):
            with self.subTest(subject_gender=subject_gender):
                plan_file = self.root / f"storage/review_queue/batch/{subject_gender}/production-plan.json"
                plan = human_review.build_plan(
                    batch_id="batch",
                    task_id="task-1",
                    stem=subject_gender,
                    audio_path="/tmp/audio.mp3",
                    script_path="/tmp/story.txt",
                    script_text="Dos personas.",
                    duration=5,
                    aspect_ratio="9:16",
                    visual_style="none",
                    editorial_profile={"subject_gender": subject_gender},
                    selection_result=selection([masculine]),
                    discovery_result=SimpleNamespace(candidates=(masculine, feminine)),
                    output_path=plan_file,
                )
                visible_uids = [plan["segments"][0]["selected_asset"]["asset_uid"]] + [
                    item["asset_uid"] for item in plan["segments"][0]["alternatives"]
                ]
                self.assertIn("asset-man", visible_uids)
                self.assertIn("asset-woman", visible_uids)

    def test_segment_backup_reorder_and_promote_updates_plan(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([candidate("asset-1", source_info={"duration": 2})]),
            discovery_result=SimpleNamespace(
                candidates=(
                    candidate("asset-1", source_info={"duration": 2}),
                    candidate("asset-2"),
                    candidate("asset-3"),
                )
            ),
            output_path=plan_file,
        )

        human_review.set_segment_backup(plan_file, "segment-001", "asset-2", True)
        plan = human_review.set_segment_backup(plan_file, "segment-001", "asset-3", True)
        self.assertEqual(
            [item["asset_uid"] for item in plan["segments"][0]["backup_assets"]],
            ["asset-2", "asset-3"],
        )

        plan = human_review.reorder_segment_backups(
            plan_file,
            "segment-001",
            ["asset-3", "asset-2"],
        )
        self.assertEqual(
            [item["asset_uid"] for item in plan["segments"][0]["backup_assets"]],
            ["asset-3", "asset-2"],
        )
        self.assertIn("target_duration", plan["segments"][0]["coverage"])

        plan = human_review.promote_segment_backup(plan_file, "segment-001", "asset-3")
        self.assertEqual(plan["segments"][0]["selected_asset"]["asset_uid"], "asset-3")
        self.assertNotIn(
            "asset-3",
            [item["asset_uid"] for item in plan["segments"][0]["backup_assets"]],
        )

    def test_segment_targets_follow_timeline_not_primary_duration(self):
        short = candidate("asset-short", source_info={"title": "mujer"}, duration=2)
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        result = selection([short])

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Una escena larga.",
            duration=9,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=result,
            discovery_result=SimpleNamespace(candidates=(short,)),
            output_path=plan_file,
        )

        coverage = plan["segments"][0]["coverage"]
        self.assertAlmostEqual(coverage["target_duration"], 9.1, places=3)
        self.assertLess(coverage["covered_duration"], coverage["target_duration"])

    def test_build_plan_assigns_audio_duration_proportionally_to_script_fragments(self):
        assets = [candidate(f"asset-{index}", duration=12) for index in range(1, 10)]
        result = MaterialSelectionResult(
            MaterialSelectionOptions("9:16", 51.49, 5),
            tuple(decision(asset) for asset in assets),
            9,
            9,
            0,
            False,
            ("term",),
            9,
        )
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        script = " ".join(
            " ".join(f"palabra{word}" for word in range(10)) + "."
            for _ in range(9)
        )

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text=script,
            duration=51.49,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=result,
            discovery_result=SimpleNamespace(candidates=tuple(assets)),
            output_path=plan_file,
        )

        durations = [segment["duration"] for segment in plan["segments"]]
        timeline = human_review.render_timeline_from_plan(plan)

        self.assertEqual(len(durations), 9)
        self.assertNotEqual(durations, [5.0] * 9)
        self.assertAlmostEqual(sum(durations), 51.49, places=6)
        self.assertAlmostEqual(sum(durations) + 0.10, timeline.required_duration, places=6)
        self.assertFalse(any(item["segment_id"] == "timeline-tail" for item in timeline.segment_shortfalls))
        self.assertEqual(plan["segments"][0]["start"], 0.0)
        for previous, current in zip(plan["segments"], plan["segments"][1:]):
            self.assertAlmostEqual(previous["end"], current["start"], places=6)
        self.assertAlmostEqual(plan["segments"][-1]["end"], 51.49, places=6)
        self.assertEqual(
            human_review.allocate_script_segment_durations(
                ["uno", "uno dos", "uno dos tres"], 12.0,
            ),
            [2.0, 4.0, 6.0],
        )

    def test_backup_can_close_missing_duration_for_a_longer_segment_target(self):
        primary = candidate("asset-primary", duration=2)
        backup = candidate("asset-backup", duration=7)
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Una escena larga.",
            duration=9,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([primary]),
            discovery_result=SimpleNamespace(candidates=(primary, backup)),
            output_path=plan_file,
        )

        self.assertGreater(plan["segments"][0]["coverage"]["missing_duration"], 0)
        plan = human_review.set_segment_backup(plan_file, "segment-001", "asset-backup", True)
        self.assertEqual(plan["segments"][0]["coverage"]["missing_duration"], 0.0)

    def test_global_missing_requires_segment_missing(self):
        assets = [candidate(f"asset-{index}", duration=2) for index in range(1, 6)]
        result = MaterialSelectionResult(
            MaterialSelectionOptions("9:16", 46.8, 5),
            tuple(decision(item) for item in assets),
            10,
            5,
            5,
            False,
            ("term",),
            10,
        )
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Uno. Dos. Tres. Cuatro. Cinco. Seis. Siete. Ocho. Nueve. Diez.",
            duration=46.8,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=result,
            discovery_result=SimpleNamespace(candidates=tuple(assets)),
            output_path=plan_file,
        )

        global_missing = plan["coverage"]["missing_duration"]
        segment_missing = sum(segment["coverage"]["missing_duration"] for segment in plan["segments"])
        segment_target = sum(segment["coverage"]["target_duration"] for segment in plan["segments"])
        self.assertGreater(global_missing, 0)
        self.assertTrue(any(segment["coverage"]["missing_duration"] > 0 for segment in plan["segments"]))
        self.assertAlmostEqual(segment_target, plan["coverage"]["target_duration"], places=3)
        self.assertAlmostEqual(segment_missing, global_missing, delta=0.01)

    def test_backup_reduces_missing_duration(self):
        primary = candidate("asset-primary", duration=2)
        backup = candidate("asset-backup", duration=2)
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Una escena.",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([primary]),
            discovery_result=SimpleNamespace(candidates=(primary, backup)),
            output_path=plan_file,
        )
        before = human_review.read_json(plan_file)["segments"][0]["coverage"]["missing_duration"]
        plan = human_review.set_segment_backup(plan_file, "segment-001", "asset-backup", True)
        after = plan["segments"][0]["coverage"]["missing_duration"]
        self.assertLess(after, before)

    def test_batch_manifest_title_exclusive_policy_has_no_generic_fallback(self):
        manifest = {"material_title": "mi-otra-yo", "source_policy": "title-exclusive"}

        policy = batch_mpt_worker._material_source_policy(manifest)

        self.assertEqual(policy["providers"]["enabled"], ("asset_hub",))
        self.assertEqual(policy["asset_hub"]["include"]["titles"], ("mi-otra-yo",))
        self.assertFalse(policy["asset_hub"]["include"]["generic"])

    def test_batch_manifest_title_exclusive_requires_title(self):
        with self.assertRaises(ValueError):
            batch_mpt_worker._material_source_policy({"source_policy": "title-exclusive"})

    def test_empty_trailing_script_segment_is_not_selected(self):
        assets = [candidate(f"asset-{index}") for index in range(3)]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Uno dos",
            duration=15,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection(assets),
            discovery_result=SimpleNamespace(candidates=tuple(assets)),
            output_path=plan_file,
        )

        self.assertEqual(len(plan["segments"]), 2)
        self.assertTrue(all(segment["script_text"].strip() for segment in plan["segments"]))
        self.assertEqual([segment["segment_id"] for segment in plan["segments"]], ["segment-001", "segment-002"])

    def test_exhausted_candidate_pool_marks_segment_for_review(self):
        asset_a = candidate("asset-a")
        asset_b = candidate("asset-b")
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Uno. Dos. Tres.",
            duration=15,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([asset_a, asset_a, asset_a]),
            discovery_result=SimpleNamespace(candidates=(asset_a, asset_b)),
            output_path=plan_file,
        )

        self.assertEqual(
            [
                segment["selected_asset"]["asset_uid"]
                if isinstance(segment.get("selected_asset"), dict)
                else ""
                for segment in plan["segments"]
            ],
            ["asset-a", "asset-b", ""],
        )
        warning = next(item for item in plan["warnings"] if item.get("type") == "review_required")
        self.assertEqual(warning["segment_id"], "segment-003")
        segment_warning = next(
            item for item in plan["segments"][2]["warnings"]
            if item.get("type") == "review_required"
        )
        self.assertEqual(segment_warning["code"], "missing_primary")

    def test_selected_and_alternatives_are_unique_and_capped_at_three(self):
        selected = candidate("asset-a")
        candidates = [
            selected,
            candidate("asset-a"),
            candidate("asset-b"),
            candidate("asset-c"),
            candidate("asset-d"),
            candidate("asset-e"),
        ]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([selected]),
            discovery_result=SimpleNamespace(candidates=tuple(candidates)),
            output_path=plan_file,
        )

        segment = plan["segments"][0]
        uids = [segment["selected_asset"]["asset_uid"]] + [
            item["asset_uid"] for item in segment["alternatives"]
        ]
        self.assertEqual(len(segment["alternatives"]), 3)
        self.assertEqual(uids, ["asset-a", "asset-b", "asset-c", "asset-d"])
        self.assertEqual(len(uids), len(set(uids)))

    def test_alternatives_are_local_and_not_consumed_by_another_segment(self):
        assets = [candidate(uid) for uid in ("asset-a", "asset-b", "asset-c", "asset-d")]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Uno. Dos.",
            duration=10,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([assets[0], assets[0]]),
            discovery_result=SimpleNamespace(candidates=tuple(assets)),
            output_path=plan_file,
        )

        second = plan["segments"][1]
        self.assertEqual(second["selected_asset"]["asset_uid"], "asset-b")
        self.assertEqual(
            [item["asset_uid"] for item in second["alternatives"]],
            ["asset-c", "asset-d", "asset-a"],
        )

    def test_alternatives_prefer_assets_not_authorized_by_other_primaries(self):
        assets = [candidate(uid) for uid in ("asset-a", "asset-b", "asset-c", "asset-d", "asset-e")]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch", task_id="task-1", stem="story", audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt", script_text="Uno. Dos.", duration=10,
            aspect_ratio="9:16", visual_style="none",
            selection_result=selection([assets[0], assets[0]]),
            discovery_result=SimpleNamespace(candidates=tuple(assets)), output_path=plan_file,
        )

        primary_uids = {
            segment["selected_asset"]["asset_uid"]
            for segment in plan["segments"]
        }
        self.assertEqual(primary_uids, {"asset-a", "asset-b"})
        for segment in plan["segments"]:
            alternative_uids = [item["asset_uid"] for item in segment["alternatives"]]
            self.assertEqual(alternative_uids, ["asset-c", "asset-d", "asset-e"])
            self.assertTrue(all(
                human_review.authorized_asset_location(
                    plan, uid, exclude_segment_id=segment["segment_id"],
                ) is None
                for uid in alternative_uids
            ))

    def test_scarce_blocked_alternative_cannot_be_promoted(self):
        assets = [candidate(uid) for uid in ("asset-a", "asset-b", "asset-c")]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        plan = human_review.build_plan(
            batch_id="batch", task_id="task-1", stem="story", audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt", script_text="Uno. Dos.", duration=10,
            aspect_ratio="9:16", visual_style="none",
            selection_result=selection([assets[0], assets[0]]),
            discovery_result=SimpleNamespace(candidates=tuple(assets)), output_path=plan_file,
        )

        first = plan["segments"][0]
        self.assertIn("asset-b", [item["asset_uid"] for item in first["alternatives"]])
        with self.assertRaisesRegex(ValueError, "already authorized in segment-002"):
            human_review.replace_segment_asset(plan_file, "segment-001", "asset-b")

    def test_script_fragments_are_contiguous_and_not_full_script(self):
        script = "Uno dos tres. Cuatro cinco seis. Siete ocho nueve. Diez once doce."
        fragments = human_review.split_script_for_segments(script, 4)

        self.assertEqual(len(fragments), 4)
        self.assertTrue(all(fragment != " ".join(script.split()) for fragment in fragments))
        self.assertEqual(" ".join(" ".join(fragments).split()), " ".join(script.split()))

    def test_script_word_fallback_preserves_order_without_duplication(self):
        words = [f"w{index:02d}" for index in range(1, 13)]
        fragments = human_review.split_script_for_segments(" ".join(words), 5)
        flattened = " ".join(fragments).split()

        self.assertEqual(len(fragments), 5)
        self.assertEqual(flattened, words)
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertTrue(all(fragment != " ".join(words) for fragment in fragments))

    def test_build_plan_assigns_segment_specific_script_fragments(self):
        selected = [candidate(f"asset-{index}") for index in range(1, 4)]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="Primero uno. Segundo dos. Tercero tres.",
            duration=15,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection(selected),
            discovery_result=SimpleNamespace(candidates=tuple(selected)),
            output_path=plan_file,
        )

        texts = [segment["script_text"] for segment in plan["segments"]]
        self.assertEqual(len(texts), 3)
        self.assertEqual(len(set(texts)), 3)
        self.assertNotIn("Primero uno. Segundo dos. Tercero tres.", texts)
        self.assertEqual(" ".join(" ".join(texts).split()), "Primero uno. Segundo dos. Tercero tres.")

    def test_relative_thumbnail_path_resolves_from_project_root(self):
        thumb = self.root / "storage/review_queue/batch/story/thumbnails/a.svg"
        thumb.parent.mkdir(parents=True)
        thumb.write_text("<svg></svg>", encoding="utf-8")

        resolved = human_review.resolve_local_asset_path(
            "storage/review_queue/batch/story/thumbnails/a.svg",
            self.root,
        )

        self.assertEqual(resolved, thumb)

    def test_container_thumbnail_path_resolves_from_host_project_root(self):
        thumb = self.root / "storage/review_queue/batch/story/thumbnails/a.svg"
        thumb.parent.mkdir(parents=True)
        thumb.write_text("<svg></svg>", encoding="utf-8")

        resolved = human_review.resolve_local_asset_path(
            "/MoneyPrinterTurbo/storage/review_queue/batch/story/thumbnails/a.svg",
            self.root,
        )

        self.assertEqual(resolved, thumb)

    def test_missing_thumbnail_resolves_to_none(self):
        self.assertIsNone(
            human_review.resolve_local_asset_path(
                "storage/review_queue/batch/story/thumbnails/missing.svg",
                self.root,
            )
        )

    def test_plan_under_project_stores_relative_thumbnail_paths(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "storage", prefix="test-review-") as temp_dir:
            plan_file = Path(temp_dir) / "review_queue/batch/story/production-plan.json"
            selected = candidate("asset-1")
            plan = human_review.build_plan(
                batch_id="batch",
                task_id="task-1",
                stem="story",
                audio_path="/tmp/audio.mp3",
                script_path="/tmp/story.txt",
                script_text="script",
                duration=5,
                aspect_ratio="9:16",
                visual_style="none",
                selection_result=selection([selected]),
                discovery_result=SimpleNamespace(candidates=(selected, candidate("asset-2"))),
                output_path=plan_file,
            )

            assets = [plan["segments"][0]["selected_asset"]] + plan["segments"][0]["alternatives"]
            for asset in assets:
                thumb = asset["thumbnail_path"]
                self.assertFalse(thumb.startswith("/opt/moneyprinterturbo/"))
                self.assertFalse(thumb.startswith("/MoneyPrinterTurbo/"))
                self.assertIsNotNone(human_review.resolve_local_asset_path(thumb, Path.cwd()))

    def test_pexels_preview_url_is_cached_before_placeholder(self):
        thumbnails = self.root / "thumbs"
        item = candidate("pexels:1", source_info={"thumbnail_url": "https://img.example/one.jpg"})

        with patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()) as get, \
            patch("app.custom.human_review.subprocess.run") as ffmpeg:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "local")
        self.assertTrue(preview["value"].endswith(".jpg"))
        self.assertEqual(warnings, [])
        self.assertTrue(Path(preview["value"]).is_file())
        get.assert_called_once()
        ffmpeg.assert_not_called()

    def test_pixabay_preview_url_falls_back_to_url_when_cache_fails(self):
        thumbnails = self.root / "thumbs"
        item = candidate("pixabay:1", provider="pixabay", source_info={"preview_url": "https://img.example/two.webp"})

        with patch("app.custom.human_review.requests.get", side_effect=RuntimeError("network")), \
            patch("app.custom.human_review.subprocess.run") as ffmpeg:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview, {"type": "url", "value": "https://img.example/two.webp", "status": "available"})
        self.assertEqual(warnings, [])
        ffmpeg.assert_not_called()

    def test_coverr_poster_preview_url_is_accepted_without_video_download(self):
        thumbnails = self.root / "thumbs"
        item = candidate(
            "coverr:1",
            provider="coverr",
            source_info={"poster": "https://img.example/poster.png"},
        )

        with patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()) as get:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "local")
        self.assertEqual(warnings, [])
        get.assert_called_once()

    def test_asset_hub_preview_available_uses_same_contract(self):
        thumbnails = self.root / "thumbs"
        item = candidate(
            "drive-a",
            provider="asset_hub",
            source_info={"preview_url": "/api/assets/drive-a/preview"},
        )

        with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
            patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()) as get:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "local")
        self.assertEqual(warnings, [])
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://asset-hub.example/api/assets/drive-a/preview")
        self.assertEqual(kwargs["headers"], {"X-Asset-Hub-Api-Key": "secret-key"})
        self.assertFalse(kwargs["allow_redirects"])

    def test_asset_hub_absolute_preview_on_configured_origin_uses_api_key(self):
        thumbnails = self.root / "thumbs"
        item = candidate(
            "drive-a",
            provider="asset_hub",
            source_info={"preview_url": "https://asset-hub.example/api/assets/drive-a/preview"},
        )

        with patch.dict(os.environ, {"ASSET_HUB_BASE_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
            patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()) as get:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "local")
        self.assertEqual(warnings, [])
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Asset-Hub-Api-Key": "secret-key"})

    def test_asset_hub_api_key_never_stored_in_production_plan(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "storage", prefix="test-review-") as temp_dir:
            plan_file = Path(temp_dir) / "review_queue/batch/story/production-plan.json"
            selected = candidate(
                "drive-a",
                provider="asset_hub",
                source_info={"preview_url": "/api/assets/drive-a/preview"},
            )
            with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
                patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()):
                plan = human_review.build_plan(
                    batch_id="batch",
                    task_id="task-1",
                    stem="story",
                    audio_path="/tmp/audio.mp3",
                    script_path="/tmp/story.txt",
                    script_text="script",
                    duration=5,
                    aspect_ratio="9:16",
                    visual_style="none",
                    selection_result=selection([selected]),
                    discovery_result=SimpleNamespace(candidates=(selected,)),
                    output_path=plan_file,
                )

            serialized = json.dumps(plan)
            self.assertNotIn("secret-key", serialized)
            self.assertNotIn("X-Asset-Hub-Api-Key", serialized)
            preview_value = plan["segments"][0]["selected_asset"]["preview"]["value"]
            self.assertFalse(Path(preview_value).is_absolute())

    def test_asset_hub_successful_jpeg_is_cached_to_relative_path(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd() / "storage", prefix="test-review-") as temp_dir:
            thumbnails = Path(temp_dir) / "review_queue/batch/story/thumbnails"
            item = candidate(
                "drive-a",
                provider="asset_hub",
                source_info={"preview_url": "/api/assets/drive-a/preview"},
            )

            with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
                patch("app.custom.human_review.requests.get", return_value=FakeHttpResponse(content_type="image/jpeg")):
                preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

            self.assertEqual(preview["type"], "local")
            self.assertTrue(preview["value"].endswith(".jpg"))
            self.assertFalse(Path(preview["value"]).is_absolute())
            self.assertTrue(human_review.resolve_local_asset_path(preview["value"], Path.cwd()).is_file())
            self.assertEqual(warnings, [])

    def test_asset_hub_404_sets_preview_none_with_warning(self):
        thumbnails = self.root / "thumbs"
        item = candidate("drive-a", provider="asset_hub", source_info={"preview_url": "/api/assets/drive-a/preview"})

        with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
            patch("app.custom.human_review.requests.get", return_value=FakeHttpResponse(status_code=404)):
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "none")
        self.assertEqual(warnings[0]["message"], "NO PREVIEW AVAILABLE")

    def test_asset_hub_401_and_403_set_preview_none_with_warning(self):
        item = candidate("drive-a", provider="asset_hub", source_info={"preview_url": "/api/assets/drive-a/preview"})
        for status in (401, 403):
            with self.subTest(status=status):
                thumbnails = self.root / f"thumbs-{status}"
                with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
                    patch("app.custom.human_review.requests.get", return_value=FakeHttpResponse(status_code=status)):
                    preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

                self.assertEqual(preview["type"], "none")
                self.assertEqual(warnings[0]["message"], "NO PREVIEW AVAILABLE")

    def test_asset_hub_network_failure_does_not_kill_review_job(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        selected = candidate("drive-a", provider="asset_hub", source_info={"preview_url": "/api/assets/drive-a/preview"})

        with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
            patch("app.custom.human_review.requests.get", side_effect=RuntimeError("network")):
            plan = human_review.build_plan(
                batch_id="batch",
                task_id="task-1",
                stem="story",
                audio_path="/tmp/audio.mp3",
                script_path="/tmp/story.txt",
                script_text="script",
                duration=5,
                aspect_ratio="9:16",
                visual_style="none",
                selection_result=selection([selected]),
                discovery_result=SimpleNamespace(candidates=(selected,)),
                output_path=plan_file,
            )

        self.assertEqual(plan["segments"][0]["selected_asset"]["preview"]["type"], "none")
        self.assertEqual(plan["warnings"][0]["message"], "NO PREVIEW AVAILABLE")

    def test_asset_hub_key_is_not_sent_to_stock_preview_urls(self):
        for provider, info in (
            ("pexels", {"thumbnail_url": "https://pexels.example/one.jpg"}),
            ("pixabay", {"preview_url": "https://pixabay.example/two.jpg"}),
            ("coverr", {"poster": "https://coverr.example/three.jpg"}),
        ):
            with self.subTest(provider=provider):
                thumbnails = self.root / f"thumbs-{provider}"
                item = candidate(f"{provider}:1", provider=provider, source_info=info)
                with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
                    patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()) as get:
                    preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

                self.assertEqual(preview["type"], "local")
                self.assertEqual(warnings, [])
                self.assertEqual(get.call_args.kwargs.get("headers"), {})

    def test_asset_hub_auth_not_sent_to_unrelated_absolute_origin(self):
        thumbnails = self.root / "thumbs"
        item = candidate(
            "drive-a",
            provider="asset_hub",
            source_info={"preview_url": "https://cdn.example/api/assets/drive-a/preview"},
        )

        with patch.dict(os.environ, {"ASSET_HUB_URL": "https://asset-hub.example", "ASSET_HUB_API_KEY": "secret-key"}, clear=False), \
            patch("app.custom.human_review.requests.get") as get:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "none")
        self.assertEqual(warnings[0]["message"], "NO PREVIEW AVAILABLE")
        get.assert_not_called()

    def test_asset_hub_without_preview_marks_only_candidate_unavailable(self):
        thumbnails = self.root / "thumbs"
        item = candidate("drive-a", provider="asset_hub")

        with patch("app.custom.human_review.requests.get") as get, \
            patch("app.custom.human_review.subprocess.run") as ffmpeg:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "none")
        self.assertEqual(preview["status"], "unavailable")
        self.assertTrue(preview["placeholder_path"].endswith(".svg"))
        self.assertEqual(warnings[0]["code"], "preview_unavailable")
        get.assert_not_called()
        ffmpeg.assert_not_called()

    def test_local_candidate_extracts_thumbnail_from_existing_file_once(self):
        video = self.root / "local.mp4"
        video.write_bytes(b"video")
        thumbnails = self.root / "thumbs"
        item = candidate("local:one", provider="local", url=video.as_posix())

        def fake_ffmpeg(args, **_kwargs):
            Path(args[-1]).write_bytes(b"jpg")

        with patch("app.custom.human_review.subprocess.run", side_effect=fake_ffmpeg) as ffmpeg:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)
            again, again_warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "local")
        self.assertEqual(warnings, [])
        self.assertEqual(again["value"], preview["value"])
        self.assertEqual(again_warnings, [])
        ffmpeg.assert_called_once()

    def test_mixed_selected_and_alternatives_use_normalized_preview_contract(self):
        selected = candidate("pexels:1", provider="pexels", source_info={"thumbnail_url": "https://img.example/one.jpg"})
        alternatives = [
            candidate("pixabay:1", provider="pixabay", source_info={"preview_url": "https://img.example/two.jpg"}),
            candidate("coverr:1", provider="coverr", source_info={"poster": "https://img.example/three.jpg"}),
            candidate("drive-a", provider="asset_hub"),
        ]
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"

        with patch("app.custom.human_review.requests.get", return_value=FakeImageResponse()):
            plan = human_review.build_plan(
                batch_id="batch",
                task_id="task-1",
                stem="story",
                audio_path="/tmp/audio.mp3",
                script_path="/tmp/story.txt",
                script_text="script",
                duration=5,
                aspect_ratio="9:16",
                visual_style="none",
                selection_result=selection([selected]),
                discovery_result=SimpleNamespace(candidates=tuple([selected] + alternatives)),
                output_path=plan_file,
            )

        assets = [plan["segments"][0]["selected_asset"]] + plan["segments"][0]["alternatives"]
        self.assertTrue(all("preview" in asset for asset in assets))
        self.assertEqual([asset["preview"]["type"] for asset in assets], ["local", "local", "local", "none"])
        self.assertEqual(plan["warnings"][0]["asset_uid"], "drive-a")

    def test_resolve_candidate_preview_is_source_agnostic(self):
        image = self.root / "thumb.jpg"
        image.write_bytes(b"jpg")
        local_candidate = {"source": "anything", "preview": {"type": "local", "value": image.as_posix()}}
        remote_candidate = {"source": "future", "preview": {"type": "url", "value": "https://img.example/future.jpg"}}
        missing_candidate = {"source": "asset_hub", "preview": {"type": "none", "value": ""}}

        self.assertEqual(human_review.resolve_candidate_preview(local_candidate), image.as_posix())
        self.assertEqual(human_review.resolve_candidate_preview(remote_candidate), "https://img.example/future.jpg")
        self.assertIsNone(human_review.resolve_candidate_preview(missing_candidate))

    def test_review_app_remains_source_agnostic(self):
        source = (Path.cwd() / "scripts/review_app.py").read_text(encoding="utf-8")

        self.assertNotIn("asset_hub", source.lower())
        self.assertNotIn("X-Asset-Hub-Api-Key", source)
        self.assertNotIn("requests.", source)

    def test_video_preview_url_does_not_download_or_materialize_video(self):
        thumbnails = self.root / "thumbs"
        item = candidate("coverr:video-preview", provider="coverr", source_info={"preview_url": "https://cdn.example/preview.mp4"})

        with patch("app.custom.human_review.requests.get") as get, \
            patch("app.custom.human_review.subprocess.run") as ffmpeg:
            preview, warnings = human_review.ensure_candidate_preview(item, thumbnails)

        self.assertEqual(preview["type"], "none")
        self.assertEqual(warnings[0]["code"], "preview_unavailable")
        get.assert_not_called()
        ffmpeg.assert_not_called()

    def test_human_review_preview_code_does_not_use_rclone_or_direct_drive(self):
        source = Path(human_review.__file__).read_text(encoding="utf-8").lower()

        self.assertNotIn("rclone", source)
        self.assertNotIn("drive_file_id", source)

    def test_replace_preserves_original_and_updates_feedback(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([candidate("asset-1")]),
            discovery_result=SimpleNamespace(candidates=(candidate("asset-1"), candidate("asset-2"))),
            output_path=plan_file,
        )

        plan = human_review.replace_segment_asset(plan_file, "segment-001", "asset-2")

        segment = plan["segments"][0]
        self.assertEqual(segment["selected_asset"]["asset_uid"], "asset-2")
        self.assertEqual(segment["original_selected_asset"]["asset_uid"], "asset-1")
        self.assertTrue(segment["feedback"]["human_changed"])

    def test_legacy_asset_without_flip_defaults_to_true(self):
        self.assertTrue(human_review.asset_flip_horizontal({"asset_uid": "legacy"}))

    def test_set_asset_flip_false_persists(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([candidate("asset-1")]),
            discovery_result=SimpleNamespace(candidates=(candidate("asset-1"),)),
            output_path=plan_file,
        )

        plan = human_review.set_asset_flip_horizontal(
            plan_file,
            "segment-001",
            "asset-1",
            False,
        )

        self.assertFalse(plan["segments"][0]["selected_asset"]["flip_horizontal"])
        reloaded = human_review.read_json(plan_file)
        self.assertFalse(reloaded["segments"][0]["selected_asset"]["flip_horizontal"])

    def test_suggested_flip_false_promoted_to_primary_stays_false(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([candidate("asset-1")]),
            discovery_result=SimpleNamespace(candidates=(candidate("asset-1"), candidate("asset-2"))),
            output_path=plan_file,
        )
        human_review.set_asset_flip_horizontal(plan_file, "segment-001", "asset-2", False)

        plan = human_review.replace_segment_asset(plan_file, "segment-001", "asset-2")

        self.assertEqual(plan["segments"][0]["selected_asset"]["asset_uid"], "asset-2")
        self.assertFalse(plan["segments"][0]["selected_asset"]["flip_horizontal"])

    def test_v2_review_hides_secondary_identity_variants_and_keeps_asset_uid(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        primary = candidate(
            "asset-original", provider="asset_hub", source_info={"provider_asset_id": "shared-source"}
        )
        variant = candidate(
            "asset-variant", provider="asset_hub", source_info={"provider_asset_id": "shared-source"}
        )
        distinct = candidate(
            "asset-distinct", provider="asset_hub", source_info={"provider_asset_id": "other-source"}
        )
        plan = human_review.build_plan(
            batch_id="batch", task_id="task-1", stem="story", audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt", script_text="scene", duration=5, aspect_ratio="9:16",
            visual_style="none", selection_result=selection([primary]),
            discovery_result=SimpleNamespace(candidates=(primary, variant, distinct)), output_path=plan_file,
        )

        segment = plan["segments"][0]
        visible = [segment["selected_asset"]] + segment["alternatives"]
        self.assertEqual(segment["selected_asset"]["asset_uid"], "asset-original")
        self.assertNotIn("asset-variant", [item["asset_uid"] for item in visible])
        self.assertIn("asset-distinct", [item["asset_uid"] for item in visible])
        approved = human_review.approve_plan(plan_file, enqueue_nightly=False)
        self.assertEqual(approved["segments"][0]["selected_asset"]["asset_uid"], "asset-original")

    def test_backup_rejects_secondary_variant_of_primary(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        primary = candidate("asset-original", provider="asset_hub", source_info={"source_url": "https://example.test/source.mp4"})
        variant = candidate("asset-variant", provider="asset_hub", source_info={"source_url": "https://example.test/source.mp4#preview"})
        human_review.write_json_atomic(plan_file, {
            "review_status": human_review.STATUS_PENDING,
            "segments": [{
                "segment_id": "segment-001",
                "selected_asset": human_review.serialize_candidate(primary),
                "original_selected_asset": human_review.serialize_candidate(primary),
                "alternatives": [human_review.serialize_candidate(variant)],
                "backup_assets": [],
            }],
        })

        with self.assertRaisesRegex(ValueError, "duplicates an existing primary"):
            human_review.set_segment_backup(plan_file, "segment-001", "asset-variant", True)

    def test_suggested_flip_false_promoted_to_backup_stays_false(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([candidate("asset-1", source_info={"duration": 3})]),
            discovery_result=SimpleNamespace(
                candidates=(
                    candidate("asset-1", source_info={"duration": 3}),
                    candidate("asset-2"),
                )
            ),
            output_path=plan_file,
        )
        human_review.set_asset_flip_horizontal(plan_file, "segment-001", "asset-2", False)

        plan = human_review.set_segment_backup(plan_file, "segment-001", "asset-2", True)

        backup = plan["segments"][0]["backup_assets"][0]
        self.assertEqual(backup["asset_uid"], "asset-2")
        self.assertFalse(backup["flip_horizontal"])

    def test_set_all_visible_flip_horizontal_updates_all_assets(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([candidate("asset-1")]),
            discovery_result=SimpleNamespace(candidates=(candidate("asset-1"), candidate("asset-2"), candidate("asset-3"))),
            output_path=plan_file,
        )

        plan = human_review.set_all_visible_flip_horizontal(plan_file, False)
        assets = [plan["segments"][0]["selected_asset"]] + plan["segments"][0]["alternatives"]
        self.assertTrue(all(asset["flip_horizontal"] is False for asset in assets))

        plan = human_review.set_all_visible_flip_horizontal(plan_file, True)
        assets = [plan["segments"][0]["selected_asset"]] + plan["segments"][0]["alternatives"]
        self.assertTrue(all(asset["flip_horizontal"] is True for asset in assets))

    def test_flip_does_not_change_coverage_or_timeline_durations(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        plan = human_review.build_plan(
            batch_id="batch",
            task_id="task-1",
            stem="story",
            audio_path="/tmp/audio.mp3",
            script_path="/tmp/story.txt",
            script_text="script",
            duration=5,
            aspect_ratio="9:16",
            visual_style="none",
            selection_result=selection([candidate("asset-1")]),
            discovery_result=SimpleNamespace(candidates=(candidate("asset-1"),)),
            output_path=plan_file,
        )
        before_coverage = human_review.coverage_summary(plan)
        before_piece = dict(human_review.render_timeline_from_plan(plan).pieces[0])

        plan["segments"][0]["selected_asset"]["flip_horizontal"] = False
        after_coverage = human_review.coverage_summary(plan)
        after_piece = dict(human_review.render_timeline_from_plan(plan).pieces[0])

        self.assertEqual(before_coverage, after_coverage)
        for key in ("segment_id", "role", "asset_uid", "source_duration", "output_duration", "playback_speed"):
            self.assertEqual(before_piece[key], after_piece[key])

    def test_approve_sets_status_and_enqueues_nightly_job(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.write_json_atomic(
            plan_file,
            {
                "schema_version": 1,
                "batch_id": "batch",
                "stem": "story",
                "task_id": "task-1",
                "duration": 5,
                "review_status": human_review.STATUS_PENDING,
                "visual_style": "none",
                "segments": [
                    {
                        "segment_id": "segment-001",
                        "duration": 5,
                        "selected_asset": {
                            "asset_uid": "asset-1",
                            "canonical_id": "asset-1",
                            "dedupe_key": "asset-1",
                            "metadata": {"duration": 5},
                        },
                        "backup_assets": [],
                    }
                ],
            },
        )

        plan = human_review.approve_plan(plan_file, project_root=self.root)

        self.assertEqual(plan["review_status"], human_review.STATUS_APPROVED)
        queued = self.root / "storage/nightly_jobs/pending/review-batch-story.json"
        self.assertTrue(queued.is_file())
        self.assertEqual(json.loads(queued.read_text())["render_mode"], human_review.RENDER_MODE)

    def test_approve_explicit_enqueue_nightly_true_enqueues(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        human_review.write_json_atomic(plan_file, {
            "schema_version": 1, "batch_id": "batch", "stem": "story", "task_id": "task-1",
            "duration": 5, "review_status": human_review.STATUS_PENDING, "visual_style": "none",
            "segments": [{"segment_id": "segment-001", "duration": 5,
                          "selected_asset": {"asset_uid": "asset-1", "canonical_id": "asset-1", "dedupe_key": "asset-1", "metadata": {"duration": 5}},
                          "backup_assets": []}],
        })

        human_review.approve_plan(plan_file, project_root=self.root, enqueue_nightly=True)

        self.assertTrue((self.root / "storage/nightly_jobs/pending/review-batch-story.json").is_file())

    def test_approve_without_enqueue_freezes_content_job_and_preserves_provenance(self):
        plan_file = self.root / "storage/review_queue/batch/story/production-plan.json"
        content_job = {"content_id": "test-content-001", "niche_id": "test-niche"}
        human_review.write_json_atomic(plan_file, {
            "schema_version": 1, "batch_id": "batch", "stem": "story", "task_id": "task-1",
            "duration": 5, "review_status": human_review.STATUS_PENDING, "visual_style": "none",
            "content_job": content_job,
            "segments": [{"segment_id": "segment-001", "duration": 5,
                          "selected_asset": {"asset_uid": "asset-1", "canonical_id": "asset-1", "dedupe_key": "asset-1", "metadata": {"duration": 5}},
                          "backup_assets": []}],
        })

        plan = human_review.approve_plan(plan_file, project_root=self.root, enqueue_nightly=False)

        self.assertEqual(plan["review_status"], human_review.STATUS_APPROVED)
        self.assertEqual(plan["content_job"], content_job)
        self.assertFalse((self.root / "storage/nightly_jobs/pending").exists())
        self.assertTrue(human_review.validate_approved_plan_integrity(plan)["ok"])

    def test_review_app_does_not_enqueue_content_job_plans(self):
        review_app = load_review_app_module()
        self.assertFalse(review_app.should_enqueue_nightly({"content_job": {"content_id": "test-content-001"}}))

    def test_review_app_keeps_legacy_enqueue_for_non_content_job_plans(self):
        review_app = load_review_app_module()
        self.assertTrue(review_app.should_enqueue_nightly({"batch_id": "legacy-batch"}))

    def test_content_id_selects_matching_content_job_plan(self):
        review_app = load_review_app_module()
        first = self.root / "first.json"
        target = self.root / "target.json"
        human_review.write_json_atomic(first, {"content_job": {"content_id": "cf_000000"}})
        human_review.write_json_atomic(target, {"content_job": {"content_id": "cf_000001"}})

        self.assertEqual(review_app.find_plan_by_content_id([first, target], "cf_000001"), target)

    def test_unknown_content_id_has_no_selected_plan(self):
        review_app = load_review_app_module()
        plan = self.root / "plan.json"
        human_review.write_json_atomic(plan, {"content_job": {"content_id": "cf_000001"}})

        self.assertIsNone(review_app.find_plan_by_content_id([plan], "cf_missing"))

    def test_absent_content_id_preserves_pending_plan_list(self):
        review_app = load_review_app_module()
        plans = [self.root / "first.json", self.root / "second.json"]

        self.assertIsNone(review_app.query_content_id())
        self.assertEqual(review_app.filter_plans_for_content_id(plans, None), plans)

    def test_legacy_plan_without_content_job_is_safe(self):
        review_app = load_review_app_module()
        legacy = self.root / "legacy.json"
        human_review.write_json_atomic(legacy, {"batch_id": "legacy-batch"})

        self.assertIsNone(review_app.plan_content_id(human_review.read_json(legacy)))
        self.assertIsNone(review_app.find_plan_by_content_id([legacy], "cf_000001"))

    def test_review_relative_url_encodes_content_id_safely(self):
        review_app = load_review_app_module()
        self.assertEqual(
            review_app.review_relative_url("cf 000/001?&"),
            "?content_id=cf+000%2F001%3F%26",
        )

    def test_approve_rejects_missing_primary_duration_and_keeps_pending(self):
        plan_file = self.root / "production-plan.json"
        plan = {
            "duration": 5,
            "review_status": human_review.STATUS_PENDING,
            "segments": [{
                "segment_id": "segment-001",
                "duration": 5,
                "selected_asset": {"asset_uid": "drive-1", "metadata": {"duration": None}},
                "backup_assets": [],
            }],
        }
        human_review.write_json_atomic(plan_file, plan)

        with self.assertRaisesRegex(ValueError, "primary drive-1 has no usable duration"):
            human_review.approve_plan(plan_file, project_root=self.root)
        self.assertEqual(
            human_review.read_json(plan_file)["review_status"],
            human_review.STATUS_PENDING,
        )

    def test_approve_rejects_backup_without_duration_when_backup_is_needed(self):
        plan_file = self.root / "production-plan.json"
        plan = {
            "duration": 5,
            "review_status": human_review.STATUS_PENDING,
            "segments": [{
                "segment_id": "segment-001",
                "duration": 5,
                "selected_asset": {"asset_uid": "drive-primary", "metadata": {"duration": 4.0}},
                "backup_assets": [{"asset_uid": "drive-backup", "metadata": {"duration": None}}],
            }],
        }
        human_review.write_json_atomic(plan_file, plan)

        with self.assertRaisesRegex(ValueError, "backup drive-backup has no usable duration"):
            human_review.approve_plan(plan_file, project_root=self.root)
        self.assertEqual(human_review.read_json(plan_file)["review_status"], human_review.STATUS_PENDING)

    def test_nightly_runner_blocks_invalid_plan_and_continues_with_next_job(self):
        queue_root = self.root / "nightly_jobs"
        paths = nightly_runner.ensure_queue_dirs(queue_root)
        bad = paths["pending"] / "bad.json"
        good = paths["pending"] / "good.json"
        bad.write_text("{}", encoding="utf-8")
        good.write_text("{}", encoding="utf-8")
        args = SimpleNamespace(project_root=self.root)
        logger = nightly_runner.Logger(self.root / "runner.log")

        with patch.object(
            nightly_preflight,
            "preflight_job_file",
            side_effect=nightly_preflight.PreflightError("approved production plan integrity failed"),
        ), patch.object(nightly_runner, "process_one_job_with_retry") as worker:
            blocked = nightly_runner.safe_process_one_job(bad, paths, args, logger)
            worker.assert_not_called()
        self.assertEqual(blocked.parent.name, "blocked")

        completed = paths["completed"] / "good"
        with patch.object(nightly_preflight, "preflight_job_file", return_value={}), patch.object(
            nightly_runner, "process_one_job_with_retry", return_value=completed
        ) as worker:
            result = nightly_runner.safe_process_one_job(good, paths, args, logger)
        self.assertEqual(result, completed)
        worker.assert_called_once()

    def test_approved_plan_is_frozen_for_replace(self):
        plan_file = self.root / "plan.json"
        human_review.write_json_atomic(
            plan_file,
            {
                "schema_version": 1,
                "review_status": human_review.STATUS_APPROVED,
                "segments": [{"segment_id": "segment-001", "selected_asset": {"asset_uid": "a"}, "alternatives": []}],
            },
        )
        with self.assertRaises(ValueError):
            human_review.replace_segment_asset(plan_file, "segment-001", "a")

    def test_materialization_adds_hflip_only_when_flip_enabled(self):
        source = self.root / "asset.mp4"
        source.write_bytes(b"video")

        def stage_with_flip(enabled):
            plan = {
                "review_status": human_review.STATUS_APPROVED,
                "duration": 4.9,
                "segments": [
                    {
                        "segment_id": "segment-001",
                        "duration": 5,
                        "selected_asset": {
                            "asset_uid": "asset-1",
                            "canonical_id": "asset-1",
                            "flip_horizontal": enabled,
                            "metadata": {"duration": 5},
                        },
                        "backup_assets": [],
                    }
                ],
            }
            selection_obj = SimpleNamespace(decisions=(decision(candidate("asset-1")),))
            acquisition = SimpleNamespace(
                materials=(
                    MaterialInfo(provider="local", url=source.as_posix(), duration=5),
                )
            )

            def fake_run(command, **_kwargs):
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(command[-1]).write_bytes(b"staged")
                return SimpleNamespace(returncode=0, stderr="")

            with patch("scripts.batch_mpt_worker.subprocess.run", side_effect=fake_run) as run:
                batch_mpt_worker._stage_human_review_timeline(
                    plan=plan,
                    selection=selection_obj,
                    acquisition=acquisition,
                    task_id=f"test-flip-{enabled}",
                )
            command = run.call_args.args[0]
            return command[command.index("-vf") + 1]

        self.assertIn("hflip", stage_with_flip(True).split(","))
        self.assertNotIn("hflip", stage_with_flip(False).split(","))

    def test_freeze_uses_a_real_final_frame_before_cloning_and_keeps_flip(self):
        source = self.root / "asset.mp4"
        source.write_bytes(b"video")
        piece = {
            "segment_id": "segment-001", "role": "FREEZE", "asset_uid": "asset-1",
            "asset": {"flip_horizontal": True}, "flip_horizontal": True,
            "source_duration": 0.04, "source_start": 3.585,
            "output_duration": 0.972, "playback_speed": 1.0, "freeze_seconds": 0.972,
        }
        timeline = SimpleNamespace(pieces=(piece,), shortfall=0, segment_shortfalls=(), total_output_duration=.972)
        selected = SimpleNamespace(decisions=(decision(candidate("asset-1")),))
        acquired = SimpleNamespace(materials=(MaterialInfo(provider="local", url=source.as_posix(), duration=4),))

        def fake_run(command, **_kwargs):
            Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[-1]).write_bytes(b"staged")
            return SimpleNamespace(returncode=0, stderr="")

        with patch.object(human_review, "render_timeline_from_plan", return_value=timeline), \
             patch("scripts.batch_mpt_worker.subprocess.run", side_effect=fake_run) as run:
            batch_mpt_worker._stage_human_review_timeline(plan={}, selection=selected, acquisition=acquired, task_id="test-freeze-filter")
        vf = run.call_args.args[0][run.call_args.args[0].index("-vf") + 1]
        self.assertIn("reverse,select=eq(n\\,0),setpts=PTS-STARTPTS,fps=24", vf)
        self.assertIn("tpad=stop_mode=clone:stop_duration=0.972000", vf)
        self.assertIn("hflip", vf.split(","))
        self.assertTrue(vf.endswith("trim=duration=0.972000,setpts=PTS-STARTPTS"))

    def test_stage_failure_includes_redacted_ffmpeg_stderr_tail(self):
        source = self.root / "asset.mp4"
        source.write_bytes(b"video")
        plan = {"review_status": human_review.STATUS_APPROVED, "duration": 5, "segments": [{
            "segment_id": "segment-001", "duration": 5,
            "selected_asset": {"asset_uid": "asset-1", "canonical_id": "asset-1", "metadata": {"duration": 5}},
            "backup_assets": [],
        }]}
        selected = SimpleNamespace(decisions=(decision(candidate("asset-1")),))
        acquired = SimpleNamespace(materials=(MaterialInfo(provider="local", url=source.as_posix(), duration=5),))
        with patch("scripts.batch_mpt_worker.subprocess.run", return_value=SimpleNamespace(returncode=1, stderr="encoder failed token=private-value\nuseful tail")):
            with self.assertRaisesRegex(RuntimeError, "useful tail") as caught:
                batch_mpt_worker._stage_human_review_timeline(plan=plan, selection=selected, acquisition=acquired, task_id="test-stderr-tail")
        self.assertIn("token=<redacted>", str(caught.exception))
        self.assertNotIn("private-value", str(caught.exception))


class TestHumanReviewPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_human_review_uses_asset_hub_reserve_when_v1_has_no_decisions(self):
        policy = {
            "providers": {"enabled": ["asset_hub"]},
            "asset_hub": {"include": {"generic": True}},
        }
        params = VideoParams(
            video_subject="culpa al descansar",
            video_script="Descansar también es necesario.",
            video_source="asset_hub",
            video_aspect="9:16",
            video_clip_duration=5,
            material_source_policy=policy,
            asset_hub_terms=["persona descansando"],
        )
        plan_file = self.root / "review" / "production-plan.json"
        object.__setattr__(
            params,
            "human_review",
            {"batch_id": "batch", "stem": "story", "production_plan_path": plan_file.as_posix()},
        )

        reserve_candidate = candidate(
            "asset-021", provider="asset_hub", term="persona descansando",
            duration=5, width=1080, height=1920, orientation="portrait",
            source_info={
                "title": "Persona descansando en casa",
                "primary_theme": "descanso",
                "primary_topic": "descansar sin culpa",
                "visual_description": "Una persona descansa tranquilamente en casa.",
                "action_description": "Descansa en silencio.",
                "contains_people": True,
                "person_visibility": "clear",
                "visual_presentation": "feminine",
            },
        )
        reserve_discovery = MaterialDiscoveryResult(
            (reserve_candidate,), (), ("asset_hub",), ("asset_hub",),
            {"stock": (), "asset_hub": ("persona descansando",)},
        )
        empty_discovery = MaterialDiscoveryResult((), (), ("asset_hub",), (), {"stock": (), "asset_hub": ()})
        selections = []
        real_empty_selection = task.empty_material_selection_result

        def capture_empty_selection(**kwargs):
            result = real_empty_selection(**kwargs)
            selections.append(result)
            return result

        with patch.object(task, "discover_material_candidates") as discover, \
             patch.object(task, "discover_asset_hub_review_reserve_candidates", return_value=reserve_discovery) as reserve, \
             patch.object(task, "discover_asset_hub_title_fallback_candidates", return_value=empty_discovery) as title_fallback, \
             patch.object(task, "select_material_candidates") as select_v1, \
             patch.object(task, "empty_material_selection_result", side_effect=capture_empty_selection), \
             patch.object(task.human_review, "ensure_candidate_preview", return_value=({"type": "none", "value": "", "status": "unavailable"}, [])), \
             patch.object(task.sm.state, "update_task") as update_task, \
             patch.object(task, "_mark_task_failed") as mark_failed:
            result = task._prepare_human_review_plan(
                "task-1", params, params.video_script, ["persona descansando"], "/tmp/audio.mp3", 5,
            )

        discover.assert_not_called()
        select_v1.assert_not_called()
        title_fallback.assert_not_called()
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].decisions, ())
        self.assertEqual(selections[0].target_count, 1)
        reserve.assert_called_once()
        self.assertEqual(reserve.call_args.kwargs["limit_per_term"], 100)
        self.assertEqual(result["review_status"], human_review.STATUS_PENDING)
        self.assertEqual(len(human_review.read_json(plan_file)["segments"]), 1)
        plan = human_review.read_json(plan_file)
        selected_asset = plan["segments"][0]["selected_asset"]
        self.assertEqual(selected_asset["asset_uid"], "asset-021")
        self.assertGreater(selected_asset["ranking_v2"]["score"], 0)
        self.assertEqual(plan["visual_ranking_version"], "candidate-ranking-v2")
        mark_failed.assert_not_called()
        update_task.assert_called_once()

    def test_human_review_multi_provider_keeps_v1_discovery(self):
        params = SimpleNamespace(
            material_source_policy={
                "providers": {"enabled": ["asset_hub", "pexels"]},
                "asset_hub": {"include": {"generic": True}},
            },
            asset_hub_terms=["tema"], video_aspect="9:16", video_clip_duration=5,
            human_review={"enabled": True}, editorial_profile={},
        )
        v1 = MaterialDiscoveryResult((candidate("pexels-001"),), (), ("pexels",), ("pexels",), {
            "stock": ("tema",), "asset_hub": (),
        })
        with patch.object(task, "discover_material_candidates", return_value=v1) as discover, \
             patch.object(task, "discover_asset_hub_review_reserve_candidates") as reserve:
            discovery, selected = task._select_autonomous_materials("task-1", params, ["tema"], 5, "guion")

        discover.assert_called_once()
        reserve.assert_not_called()
        self.assertEqual(discovery.candidates, v1.candidates)
        self.assertEqual(selected.decisions[0].candidate.canonical_id, "pexels-001")

    def test_generales_multi_provider_review_keeps_v1_candidates_in_plan(self):
        # This is GENERALES' resolved open policy, not an asset-profile branch.
        policy = {
            "providers": {"enabled": ["asset_hub", "pexels", "pixabay", "coverr", "local"]},
            "asset_hub": {"include": {"generic": True}},
        }
        params = VideoParams(
            video_subject="tema", video_script="Un guion corto.", video_aspect="9:16",
            video_clip_duration=5, material_source_policy=policy,
        )
        plan_file = self.root / "review" / "production-plan.json"
        object.__setattr__(params, "human_review", {
            "batch_id": "batch", "stem": "story", "production_plan_path": plan_file.as_posix(),
        })
        primary = candidate("pexels-001", provider="pexels")
        alternative = candidate("pixabay-002", provider="pixabay")
        v1 = MaterialDiscoveryResult((primary, alternative), (), ("pexels", "pixabay"), ("pexels", "pixabay"), {
            "stock": ("tema",), "asset_hub": (),
        })

        with patch.object(task, "discover_material_candidates", return_value=v1) as discover, \
             patch.object(task, "discover_asset_hub_review_reserve_candidates") as reserve, \
             patch.object(task.human_review, "ensure_candidate_preview", return_value=({"type": "none", "value": "", "status": "unavailable"}, [])), \
             patch.object(task.sm.state, "update_task"):
            result = task._prepare_human_review_plan(
                "task-1", params, params.video_script, ["tema"], "/tmp/audio.mp3", 5,
            )

        discover.assert_called_once()
        reserve.assert_not_called()
        self.assertEqual(result["review_status"], human_review.STATUS_PENDING)
        segment = human_review.read_json(plan_file)["segments"][0]
        self.assertEqual(segment["selected_asset"]["asset_uid"], "pexels-001")
        self.assertEqual([item["asset_uid"] for item in segment["alternatives"]], ["pixabay-002"])
        diagnostics = human_review.read_json(plan_file)["provider_diagnostics"]
        self.assertEqual(
            [item["provider"] for item in diagnostics],
            ["asset_hub", "pexels", "pixabay", "coverr", "local"],
        )
        self.assertEqual(next(item for item in diagnostics if item["provider"] == "pixabay")["review_visible_count"], 1)

    def test_review_plan_completes_with_stock_pool_when_asset_hub_is_unavailable(self):
        policy = {
            "providers": {"enabled": ["asset_hub", "pexels", "pixabay"]},
            "asset_hub": {"include": {"generic": True}},
        }
        params = VideoParams(
            video_subject="tema", video_script="Un guion corto.", video_aspect="9:16",
            video_clip_duration=5, material_source_policy=policy,
        )
        plan_file = self.root / "review" / "production-plan.json"
        object.__setattr__(params, "human_review", {
            "batch_id": "batch", "stem": "story", "production_plan_path": plan_file.as_posix(),
        })
        stock_pool = (candidate("pexels-001", provider="pexels"), candidate("pixabay-002", provider="pixabay"))
        discovery = MaterialDiscoveryResult(
            stock_pool,
            (DiscoveryDiagnostic("asset_hub", "tema", "unavailable", "KurukinAssetHubUnavailableError: circuit open", 0),),
            ("asset_hub", "pexels", "pixabay"),
            ("pexels", "pixabay"),
            {"stock": ("tema",), "asset_hub": ("tema",)},
        )
        with patch.object(task, "_select_autonomous_materials", return_value=(discovery, selection(stock_pool))), \
             patch.object(task.human_review, "ensure_candidate_preview", return_value=({"type": "none", "value": "", "status": "unavailable"}, [])), \
             patch.object(task.sm.state, "update_task"):
            result = task._prepare_human_review_plan(
                "task-1", params, params.video_script, ["tema"], "/tmp/audio.mp3", 5,
            )

        self.assertEqual(result["review_status"], human_review.STATUS_PENDING)
        plan = human_review.read_json(plan_file)
        self.assertNotIn("asset_hub", [
            asset["source"] for segment in plan["segments"]
            for asset in [segment["selected_asset"], *segment["alternatives"]]
            if asset
        ])
        asset_hub = next(item for item in plan["provider_diagnostics"] if item["provider"] == "asset_hub")
        self.assertEqual(asset_hub["status"], "unavailable")
        self.assertEqual(asset_hub["candidate_count"], 0)

    def test_autonomous_selection_does_not_use_review_reserve(self):
        params = SimpleNamespace(
            material_source_policy={"providers": {"enabled": ["pexels"]}},
            asset_hub_terms=[], video_aspect="9:16", video_clip_duration=5,
            human_review=False,
        )
        v1 = MaterialDiscoveryResult((candidate("pexels-001"),), (), ("pexels",), ("pexels",), {
            "stock": ("tema",), "asset_hub": (),
        })
        with patch.object(task, "discover_material_candidates", return_value=v1) as discover, \
             patch.object(task, "discover_asset_hub_review_reserve_candidates") as reserve:
            _discovery, selected = task._select_autonomous_materials("task-1", params, ["tema"], 5)

        discover.assert_called_once()
        reserve.assert_not_called()
        self.assertEqual(selected.decisions[0].candidate.canonical_id, "pexels-001")

    def test_human_review_fails_closed_when_ten_segments_have_no_candidates(self):
        params = VideoParams(
            video_subject="tema", material_source_policy={"providers": {"enabled": ["pexels"]}},
        )
        object.__setattr__(params, "human_review", {"batch_id": "batch", "stem": "story"})
        empty_selection = MaterialSelectionResult(
            MaterialSelectionOptions("9:16", 50, 5), (), 10, 0, 10, False, (), 0,
        )
        empty_plan = {
            "review_status": human_review.STATUS_PENDING,
            "segments": [{"selected_asset": None, "alternatives": []} for _ in range(10)],
        }
        failed = {"error": "human review preparation produced no usable candidates"}

        with patch.object(task, "_select_autonomous_materials", return_value=(SimpleNamespace(candidates=()), empty_selection)), \
             patch.object(task.human_review, "build_plan", return_value=empty_plan), \
             patch.object(task, "_mark_task_failed", return_value=failed) as mark_failed, \
             patch.object(task.sm.state, "update_task") as update_task:
            result = task._prepare_human_review_plan(
                "task-1", params, "guion", ["tema"], "/tmp/audio.mp3", 50,
            )

        self.assertEqual(result, failed)
        mark_failed.assert_called_once_with(
            "task-1", "review", "human review preparation produced no usable candidates",
        )
        update_task.assert_not_called()

    def test_empty_v1_selection_without_human_review(self):
        params = VideoParams(
            video_subject="story",
            video_source="asset_hub",
            video_aspect="9:16",
            video_clip_duration=5,
            material_source_policy={
                "providers": {"enabled": ["asset_hub"]},
                "asset_hub": {"include": {"generic": True}},
            },
        )
        object.__setattr__(params, "human_review", False)
        empty_selection = MaterialSelectionResult(
            MaterialSelectionOptions("9:16", 5, 5), (), 1, 0, 1, False, (), 0,
        )
        failed = {"error": "No usable visual materials found"}

        with patch.object(task, "_select_autonomous_materials", return_value=(SimpleNamespace(candidates=()), empty_selection)), \
             patch.object(task, "_mark_task_failed", return_value=failed) as mark_failed, \
             patch.object(task.human_review, "build_plan") as build_plan:
            result = task._prepare_human_review_plan(
                "task-1", params, "script", ["term"], "/tmp/audio.mp3", 5,
            )

        self.assertEqual(result, failed)
        mark_failed.assert_called_once_with("task-1", "materials", "No usable visual materials found")
        build_plan.assert_not_called()

    def test_stop_at_review_does_not_generate_subtitles_or_video(self):
        params = VideoParams(
            video_subject="story",
            video_script="script",
            video_source="pexels",
            material_source_policy={"providers": {"enabled": ["pexels"]}},
            custom_audio_file="/tmp/audio.mp3",
            subtitle_enabled=False,
        )
        object.__setattr__(
            params,
            "human_review",
            {"batch_id": "batch", "stem": "story", "production_plan_path": "/tmp/plan.json"},
        )
        selected = candidate("asset-1")
        with patch.object(task, "generate_script", return_value="script"), \
            patch.object(task, "generate_terms", return_value=["term"]), \
            patch.object(task, "save_script_data"), \
            patch.object(task, "generate_audio", return_value=("/tmp/audio.mp3", 5, None)), \
            patch.object(task, "_select_autonomous_materials", return_value=(SimpleNamespace(candidates=(selected,)), selection([selected]))), \
            patch.object(task.human_review, "build_plan", return_value={
                "review_status": human_review.STATUS_PENDING,
                "segments": [{"selected_asset": {"asset_uid": "asset-1"}, "alternatives": []}],
            }) as build_plan, \
            patch.object(task, "generate_subtitle") as generate_subtitle, \
            patch.object(task, "generate_final_videos") as generate_final_videos, \
            patch.object(task.sm.state, "update_task"):
            result = task._run_pipeline("task-1", params, stop_at="review")

        self.assertEqual(result["review_status"], human_review.STATUS_PENDING)
        build_plan.assert_called_once()
        generate_subtitle.assert_not_called()
        generate_final_videos.assert_not_called()


class TestHumanReviewNightRunner(unittest.TestCase):
    def test_nightly_runner_detects_human_review_jobs(self):
        job = {"render_mode": human_review.RENDER_MODE, "production_plan_path": "/tmp/plan.json"}
        self.assertTrue(nightly_runner.is_human_review_batch_job(job))
        self.assertEqual(nightly_runner.validate_job(job)["production_plan_path"], "/tmp/plan.json")

    def test_preflight_valid_human_review_job_returns_empty_warnings(self):
        job = {
            "render_mode": human_review.RENDER_MODE,
            "task_id": "task-1",
            "production_plan_path": "/tmp/approved-plan.json",
        }
        plan = {
            "review_status": human_review.STATUS_APPROVED,
            "task_id": "task-1",
            "audio_path": "/tmp/audio.mp3",
        }
        timeline = SimpleNamespace(
            shortfall=0.0,
            pieces=(object(),),
            total_output_duration=5.0,
        )
        selected = SimpleNamespace(decisions=(object(),))

        with patch.object(nightly_preflight, "project_path", return_value=Path("/tmp/mock")), \
             patch.object(nightly_preflight, "require_file"), \
             patch.object(nightly_preflight, "read_json", return_value=plan), \
             patch.object(nightly_preflight, "_plan_script_available", return_value=True), \
             patch.object(
                 human_review,
                 "validate_approved_plan_integrity",
                 return_value={"ok": True, "errors": [], "coverage": {}, "segment_coverage": {}, "stored_coverage": {}},
             ), \
             patch.object(human_review, "render_timeline_from_plan", return_value=timeline), \
             patch.object(human_review, "selection_result_from_plan", return_value=selected):
            report = nightly_preflight.validate_human_review_job(
                job,
                materialize=False,
            )

        self.assertEqual(report["warnings"], [])


class TestProduceBatchHumanReviewFlag(unittest.TestCase):
    def test_default_cli_does_not_enable_human_review(self):
        args = produce_batch.build_parser().parse_args(["storage/batch_inputs/lote-001"])
        self.assertFalse(args.human_review)

    def test_human_review_cli_flag_is_opt_in(self):
        args = produce_batch.build_parser().parse_args(["storage/batch_inputs/lote-001", "--human-review"])
        self.assertTrue(args.human_review)


if __name__ == "__main__":
    unittest.main()
