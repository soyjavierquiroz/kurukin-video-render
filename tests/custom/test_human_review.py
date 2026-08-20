import json
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.custom import human_review
from app.custom.material_discovery import MaterialCandidate
from app.custom.material_selection import MaterialSelectionDecision, MaterialSelectionOptions, MaterialSelectionResult
from app.models.schema import VideoParams
from app.models.schema import MaterialInfo
from scripts import batch_mpt_worker
from scripts import nightly_runner
from scripts import produce_batch

TASK_DEPS_AVAILABLE = importlib.util.find_spec("openai") is not None
if TASK_DEPS_AVAILABLE:
    from app.services import task


def candidate(uid, term="term", provider="pexels", url=None, source_info=None):
    return MaterialCandidate(
        provider=provider,
        canonical_id=uid,
        dedupe_key=uid,
        search_term=term,
        rank=1,
        url=url if url is not None else f"https://example.test/{uid}.mp4",
        duration=5,
        width=1080,
        height=1920,
        orientation="portrait",
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
        self.assertLessEqual(len(plan["segments"][0]["alternatives"]), 3)
        self.assertTrue(Path(plan["segments"][0]["selected_asset"]["thumbnail_path"]).exists())
        self.assertTrue(plan["segments"][0]["selected_asset"]["flip_horizontal"])

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

    def test_exhausted_candidate_pool_allows_repeat_with_warning(self):
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
            [segment["selected_asset"]["asset_uid"] for segment in plan["segments"]],
            ["asset-a", "asset-b", "asset-a"],
        )
        warning = next(item for item in plan["warnings"] if item.get("type") == "forced_asset_repeat")
        self.assertEqual(warning["segment_id"], "segment-003")
        self.assertEqual(warning["asset_uid"], "asset-a")
        segment_warning = next(
            item for item in plan["segments"][2]["warnings"]
            if item.get("type") == "forced_asset_repeat"
        )
        self.assertEqual(segment_warning["asset_uid"], "asset-a")

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

    def test_rank_order_is_preserved_except_used_selected_asset(self):
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
                "review_status": human_review.STATUS_PENDING,
                "visual_style": "none",
                "segments": [],
            },
        )

        plan = human_review.approve_plan(plan_file, project_root=self.root)

        self.assertEqual(plan["review_status"], human_review.STATUS_APPROVED)
        queued = self.root / "storage/nightly_jobs/pending/review-batch-story.json"
        self.assertTrue(queued.is_file())
        self.assertEqual(json.loads(queued.read_text())["render_mode"], human_review.RENDER_MODE)

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


class TestHumanReviewPipeline(unittest.TestCase):
    @unittest.skipUnless(TASK_DEPS_AVAILABLE, "task service optional dependencies are not installed")
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
            patch.object(task.human_review, "build_plan", return_value={"review_status": human_review.STATUS_PENDING}) as build_plan, \
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


class TestProduceBatchHumanReviewFlag(unittest.TestCase):
    def test_default_cli_does_not_enable_human_review(self):
        args = produce_batch.build_parser().parse_args(["storage/batch_inputs/lote-001"])
        self.assertFalse(args.human_review)

    def test_human_review_cli_flag_is_opt_in(self):
        args = produce_batch.build_parser().parse_args(["storage/batch_inputs/lote-001", "--human-review"])
        self.assertTrue(args.human_review)


if __name__ == "__main__":
    unittest.main()
