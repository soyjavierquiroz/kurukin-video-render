import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.asset_hub_manifest import convert_asset_hub_manifest_to_materials
from app.custom.kurukin_asset_hub_wiring import (
    REASON_EXPLICIT_ASSET_SELECTION_REQUIRED,
    STATUS_NEEDS_INPUT,
    KurukinAssetHubMaterializationNotReady,
    KurukinAssetHubSelectionRequired,
    KurukinAssetHubWiringError,
    _is_ready_response,
    build_asset_hub_bundle_scenes,
    build_asset_hub_search_requests,
    build_missing_selection_result,
    resolve_renderer_manifest_path,
    search_asset_hub_candidates,
    validate_explicit_manifest_selection,
    wire_explicit_asset_hub_bundle,
)
from app.custom.kurukin_job_adapter import build_moneyprinter_payload
from app.custom import kurukin_job_adapter
from app.custom.kurukin_job_intent import compile_job_intent_to_mpt_spec


def make_intent() -> dict:
    return {
        "task_id": "mpt-001",
        "mode": "topic_to_video",
        "topic": "Mi otra yo",
        "scenes": [
            {
                "index": 1,
                "duration_seconds": 5,
                "text": "Una mujer camina hacia la luz.",
                "visual_keywords": ["mujer luz"],
            },
            {
                "index": 2,
                "duration_seconds": 5,
                "text": "La ciudad aparece en silencio.",
                "visual_keywords": [],
            },
        ],
    }


def generic_policy() -> dict:
    return {"sources": [{"scope": "generic"}]}


class FakeProvider:
    def __init__(
        self,
        *,
        search_results=None,
        create_response=None,
        materialize_response=None,
        manifest=None,
    ):
        self.search_results = list(search_results or [])
        self.create_response = create_response or {"bundle_uid": "jab_test"}
        self.materialize_response = materialize_response or {"status": "ready"}
        self.manifest = manifest or {}
        self.search_calls = []
        self.create_calls = []
        self.materialize_calls = []
        self.manifest_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.search_results.pop(0) if self.search_results else []

    def create_bundle(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.create_response

    def materialize_bundle(self, bundle_uid, *, force=False):
        self.materialize_calls.append({"bundle_uid": bundle_uid, "force": force})
        return self.materialize_response

    def get_renderer_manifest(self, bundle_uid):
        self.manifest_calls.append({"bundle_uid": bundle_uid})
        return self.manifest


class TestKurukinAssetHubWiring(unittest.TestCase):
    def test_scene_to_search_request(self):
        requests = build_asset_hub_search_requests(make_intent(), generic_policy())

        self.assertEqual(requests[0]["scene_id"], "scene-001")
        self.assertEqual(requests[0]["scene_index"], 1)
        self.assertEqual(requests[0]["script_scene"], "Una mujer camina hacia la luz.")

    def test_visual_keywords_used_as_query(self):
        requests = build_asset_hub_search_requests(make_intent(), generic_policy())

        self.assertEqual(requests[0]["query"], "mujer luz")

    def test_fallback_to_scene_text(self):
        requests = build_asset_hub_search_requests(make_intent(), generic_policy())

        self.assertEqual(requests[1]["query"], "La ciudad aparece en silencio.")

    def test_source_policy_preserved_exactly(self):
        policy = {"sources": [{"scope": "title", "title": "mi-otra-yo"}, {"scope": "generic"}]}

        requests = build_asset_hub_search_requests(make_intent(), policy)

        self.assertEqual(requests[0]["source_policy"], policy)

    def test_generic_policy(self):
        requests = build_asset_hub_search_requests(make_intent(), generic_policy())

        self.assertEqual(requests[0]["source_policy"], {"sources": [{"scope": "generic"}]})

    def test_title_mi_otra_yo_policy(self):
        policy = {"sources": [{"scope": "title", "title": "mi-otra-yo"}]}

        requests = build_asset_hub_search_requests(make_intent(), policy)

        self.assertEqual(requests[0]["source_policy"], policy)

    def test_title_plus_generic_policy(self):
        policy = {"sources": [{"scope": "title", "title": "mi-otra-yo"}, {"scope": "generic"}]}

        requests = build_asset_hub_search_requests(make_intent(), policy)

        self.assertEqual(requests[0]["source_policy"]["sources"], policy["sources"])

    def test_brand_grandiosa_mujer_policy(self):
        policy = {"sources": [{"scope": "brand", "brand": "grandiosa-mujer"}]}

        requests = build_asset_hub_search_requests(make_intent(), policy)

        self.assertEqual(requests[0]["source_policy"], policy)

    def test_search_produces_candidates_by_scene(self):
        provider = FakeProvider(
            search_results=[
                [
                    {
                        "asset_uid": "drive-a",
                        "filename": "a.mp4",
                        "type": "video",
                        "scope": "generic",
                        "orientation": "vertical",
                        "tags": ["light"],
                    }
                ],
                [],
            ]
        )
        requests = build_asset_hub_search_requests(make_intent(), generic_policy())

        result = search_asset_hub_candidates(provider, requests)

        scenes = result["asset_hub_selection"]["scenes"]
        self.assertEqual(scenes[0]["candidates"][0]["asset_uid"], "drive-a")
        self.assertEqual(scenes[0]["candidates"][0]["dedupe_key"], "kurukin_media:drive-a")

    def test_count_zero_is_valid_candidates_empty(self):
        provider = FakeProvider(search_results=[[], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        self.assertEqual(result["asset_hub_selection"]["scenes"][0]["candidates"], [])
        self.assertFalse(result["candidates_available"])

    def test_search_never_autoselects(self):
        provider = FakeProvider(search_results=[[{"asset_uid": "drive-a"}], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        self.assertEqual(result["asset_hub_selection"]["selected_asset_uids"], {})

    def test_search_candidate_preserves_production_plan_metadata_contract(self):
        metadata = {
            "duration": 7.1,
            "width": 1080,
            "height": 1350,
            "orientation": "vertical-4x5",
            "people_count": 1,
            "visual_presentation": "feminine",
            "visual_presentation_confidence": 0.98,
            "person_visibility": "clear",
            "primary_topic": "aceptar ayuda",
            "primary_theme": "vulnerabilidad",
        }
        provider = FakeProvider(search_results=[[{"asset_uid": "drive-a", **metadata}], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        candidate = result["asset_hub_selection"]["scenes"][0]["candidates"][0]
        self.assertEqual({key: candidate[key] for key in metadata}, metadata)

    def test_search_result_is_needs_input_not_ok_after_search(self):
        provider = FakeProvider(search_results=[[], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], STATUS_NEEDS_INPUT)
        self.assertTrue(result["search_complete"])

    def test_missing_explicit_selection_is_needs_input(self):
        search_result = {
            "search_complete": True,
            "candidates_available": True,
            "asset_hub_selection": {"scenes": []},
        }

        result = build_missing_selection_result(search_result)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], STATUS_NEEDS_INPUT)
        self.assertEqual(result["reason"], REASON_EXPLICIT_ASSET_SELECTION_REQUIRED)

    def test_selected_asset_uids_preserve_order(self):
        scenes = build_asset_hub_bundle_scenes(
            make_intent(),
            {"scene-001": ["drive-b", "drive-a"], "scene-002": ["drive-c"]},
        )

        self.assertEqual(scenes[0]["selected_asset_uids"], ["drive-b", "drive-a"])

    def test_selected_asset_uids_integer_rejected(self):
        with self.assertRaises(Exception):
            build_asset_hub_bundle_scenes(
                make_intent(),
                {"scene-001": [123], "scene-002": ["drive-c"]},
            )

    def test_scene_required_without_selection_blocks_bundle(self):
        provider = FakeProvider()

        with self.assertRaises(KurukinAssetHubSelectionRequired):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"]},
            )

        self.assertEqual(provider.create_calls, [])

    def test_create_bundle_receives_exact_explicit_scenes(self):
        manifest = self._manifest_for_root("/data/job-assets")
        provider = FakeProvider(manifest=manifest)

        wire_explicit_asset_hub_bundle(
            make_intent(),
            provider,
            {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
        )

        scenes = provider.create_calls[0]["scenes"]
        self.assertEqual(scenes[0]["selected_asset_uids"], ["drive-a"])
        self.assertNotIn("brand_slug", provider.create_calls[0])

    def test_exact_manifest_selection_with_both_scenes_is_ok(self):
        manifest = self._manifest_for_root("/data/job-assets")
        provider = FakeProvider(manifest=manifest)

        result = wire_explicit_asset_hub_bundle(
            make_intent(),
            provider,
            {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
        )

        self.assertEqual(result["asset_hub"]["bundle_uid"], "jab_test")

    def test_exact_manifest_selection_missing_scene_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"] = manifest["scenes"][:1]
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

        self.assertEqual(len(provider.create_calls), 1)
        self.assertEqual(len(provider.materialize_calls), 1)

    def test_exact_manifest_selection_missing_asset_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"][1]["assets"] = []
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

    def test_exact_manifest_selection_wrong_asset_uid_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"][1]["assets"][0]["asset_uid"] = "drive-x"
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

    def test_exact_manifest_selection_extra_asset_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        extra = dict(manifest["scenes"][1]["assets"][0])
        extra["asset_uid"] = "drive-extra"
        extra["filename"] = "clip-extra.mp4"
        extra["local_path"] = "/data/job-assets/jab_test/scene-002/clip-extra.mp4"
        manifest["scenes"][1]["assets"].append(extra)
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

    def test_exact_manifest_selection_extra_scene_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"].append(
            {
                "scene_id": "scene-003",
                "scene_index": 3,
                "assets": [
                    {
                        "asset_uid": "drive-c",
                        "status": "ready",
                        "type": "video",
                        "filename": "clip-c.mp4",
                        "local_path": "/data/job-assets/jab_test/scene-003/clip-c.mp4",
                    }
                ],
            }
        )
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

    def test_exact_manifest_selection_duplicate_manifest_scene_id_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        duplicate = dict(manifest["scenes"][1])
        duplicate["scene_index"] = 3
        manifest["scenes"].append(duplicate)
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

    def test_exact_manifest_selection_duplicate_expected_scene_id_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        expected = [
            {
                "scene_id": "scene-001",
                "scene_index": 1,
                "script_scene": "one",
                "selected_asset_uids": ["drive-a"],
            },
            {
                "scene_id": "scene-001",
                "scene_index": 2,
                "script_scene": "two",
                "selected_asset_uids": ["drive-a"],
            },
        ]

        with self.assertRaises(KurukinAssetHubWiringError):
            validate_explicit_manifest_selection(manifest, expected)

    def test_exact_manifest_selection_without_rank_preserves_list_order(self):
        intent = make_intent()
        intent["scenes"] = intent["scenes"][:1]
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"] = manifest["scenes"][:1]
        manifest["scenes"][0]["assets"] = [
            {
                "asset_uid": "drive-b",
                "status": "ready",
                "type": "video",
                "filename": "clip-b.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-b.mp4",
            },
            {
                "asset_uid": "drive-a",
                "status": "ready",
                "type": "video",
                "filename": "clip-a.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-a.mp4",
            },
        ]
        provider = FakeProvider(manifest=manifest)

        result = wire_explicit_asset_hub_bundle(
            intent,
            provider,
            {"scene-001": ["drive-b", "drive-a"]},
        )

        self.assertEqual(result["asset_hub"]["bundle_uid"], "jab_test")

    def test_exact_manifest_selection_order_from_rank_is_ok(self):
        intent = make_intent()
        intent["scenes"] = intent["scenes"][:1]
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"] = manifest["scenes"][:1]
        manifest["scenes"][0]["assets"] = [
            {
                "asset_uid": "drive-a",
                "status": "ready",
                "type": "video",
                "filename": "clip-a.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-a.mp4",
                "rank": 2,
            },
            {
                "asset_uid": "drive-b",
                "status": "ready",
                "type": "video",
                "filename": "clip-b.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-b.mp4",
                "rank": 1,
            },
        ]
        provider = FakeProvider(manifest=manifest)

        result = wire_explicit_asset_hub_bundle(
            intent,
            provider,
            {"scene-001": ["drive-b", "drive-a"]},
        )

        self.assertEqual(result["asset_hub"]["bundle_uid"], "jab_test")

    def test_exact_manifest_selection_rank_order_mismatch_fails(self):
        intent = make_intent()
        intent["scenes"] = intent["scenes"][:1]
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"] = manifest["scenes"][:1]
        manifest["scenes"][0]["assets"] = [
            {
                "asset_uid": "drive-a",
                "status": "ready",
                "type": "video",
                "filename": "clip-a.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-a.mp4",
                "rank": 1,
            },
            {
                "asset_uid": "drive-b",
                "status": "ready",
                "type": "video",
                "filename": "clip-b.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-b.mp4",
                "rank": 2,
            },
        ]
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                intent,
                provider,
                {"scene-001": ["drive-b", "drive-a"]},
            )

    def test_exact_manifest_selection_partial_ranks_fail(self):
        intent = make_intent()
        intent["scenes"] = intent["scenes"][:1]
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"] = manifest["scenes"][:1]
        manifest["scenes"][0]["assets"] = [
            {
                "asset_uid": "drive-a",
                "status": "ready",
                "type": "video",
                "filename": "clip-a.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-a.mp4",
                "rank": 1,
            },
            {
                "asset_uid": "drive-b",
                "status": "ready",
                "type": "video",
                "filename": "clip-b.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-b.mp4",
            },
        ]
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                intent,
                provider,
                {"scene-001": ["drive-a", "drive-b"]},
            )

    def test_exact_manifest_selection_duplicate_ranks_fail(self):
        intent = make_intent()
        intent["scenes"] = intent["scenes"][:1]
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"] = manifest["scenes"][:1]
        manifest["scenes"][0]["assets"] = [
            {
                "asset_uid": "drive-a",
                "status": "ready",
                "type": "video",
                "filename": "clip-a.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-a.mp4",
                "rank": 1,
            },
            {
                "asset_uid": "drive-b",
                "status": "ready",
                "type": "video",
                "filename": "clip-b.mp4",
                "local_path": "/data/job-assets/jab_test/scene-001/clip-b.mp4",
                "rank": 1,
            },
        ]
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                intent,
                provider,
                {"scene-001": ["drive-a", "drive-b"]},
            )

    def test_exact_manifest_selection_non_ready_selected_asset_fails(self):
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["scenes"][1]["assets"][0]["status"] = "pending"
        provider = FakeProvider(manifest=manifest)

        with self.assertRaises(KurukinAssetHubWiringError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

    def test_exact_manifest_selection_asset_materialization_status_ready(self):
        manifest = self._single_scene_manifest_with_statuses(
            asset_statuses={"materialization_status": " ready "}
        )

        validate_explicit_manifest_selection(
            manifest,
            [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
        )

    def test_exact_manifest_selection_asset_status_ready_without_materialization_status(self):
        manifest = self._single_scene_manifest_with_statuses(
            asset_statuses={"status": " READY "}
        )

        validate_explicit_manifest_selection(
            manifest,
            [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
        )

    def test_exact_manifest_selection_inherits_scene_materialization_status_ready(self):
        manifest = self._single_scene_manifest_with_statuses(
            scene_statuses={"materialization_status": " ready "}
        )

        validate_explicit_manifest_selection(
            manifest,
            [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
        )

    def test_exact_manifest_selection_inherits_manifest_materialization_status_ready(self):
        manifest = self._single_scene_manifest_with_statuses(
            manifest_statuses={"materialization_status": " ready "}
        )

        validate_explicit_manifest_selection(
            manifest,
            [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
        )

    def test_exact_manifest_selection_asset_pending_overrides_manifest_ready(self):
        manifest = self._single_scene_manifest_with_statuses(
            manifest_statuses={"materialization_status": "ready"},
            asset_statuses={"materialization_status": "pending"},
        )

        with self.assertRaises(KurukinAssetHubWiringError):
            validate_explicit_manifest_selection(
                manifest,
                [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
            )

    def test_exact_manifest_selection_asset_failed_overrides_scene_ready(self):
        manifest = self._single_scene_manifest_with_statuses(
            scene_statuses={"materialization_status": "ready"},
            asset_statuses={"status": "failed"},
        )

        with self.assertRaises(KurukinAssetHubWiringError):
            validate_explicit_manifest_selection(
                manifest,
                [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
            )

    def test_exact_manifest_selection_without_any_status_is_not_ready(self):
        manifest = self._single_scene_manifest_with_statuses()

        with self.assertRaises(KurukinAssetHubWiringError):
            validate_explicit_manifest_selection(
                manifest,
                [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
            )

    def test_exact_manifest_selection_real_manifest_bundle_scene_ready_passes(self):
        manifest = self._single_scene_manifest_with_statuses(
            manifest_statuses={"materialization_status": "ready", "status": "ready"},
            scene_statuses={"materialization_status": "ready", "status": "ready"},
        )

        validate_explicit_manifest_selection(
            manifest,
            [{"scene_id": "scene-001", "selected_asset_uids": ["drive-882918f4"]}],
        )

    def test_materialize_uses_force_false(self):
        manifest = self._manifest_for_root("/data/job-assets")
        provider = FakeProvider(manifest=manifest)

        wire_explicit_asset_hub_bundle(
            make_intent(),
            provider,
            {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
        )

        self.assertEqual(provider.materialize_calls[0], {"bundle_uid": "jab_test", "force": False})

    def test_stale_manifest_retries_exact_frozen_selection_once(self):
        valid = self._manifest_for_root("/data/job-assets")
        stale = json.loads(json.dumps(valid))
        stale["scenes"][0]["assets"][0]["asset_uid"] = "wrong-approved-never"
        provider = FakeProvider(manifest=valid)
        manifests = iter((stale, valid))
        provider.get_renderer_manifest = lambda _bundle_uid: next(manifests)

        wire_explicit_asset_hub_bundle(
            make_intent(), provider,
            {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
        )

        self.assertEqual(len(provider.create_calls), 2)
        self.assertEqual([call["force"] for call in provider.materialize_calls], [False, True])
        self.assertEqual(
            provider.create_calls[1]["scenes"][0]["selected_asset_uids"], ["drive-a"],
        )
        self.assertEqual(provider.create_calls[1]["scenes"][0]["scene_id"], "scene-001")

    def test_stale_manifest_retry_still_mismatching_blocks(self):
        stale = self._manifest_for_root("/data/job-assets")
        stale["scenes"][0]["assets"][0]["asset_uid"] = "wrong-approved-never"
        provider = FakeProvider(manifest=stale)

        with self.assertRaisesRegex(KurukinAssetHubWiringError, "does not match explicit"):
            wire_explicit_asset_hub_bundle(
                make_intent(), provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )
        self.assertEqual(len(provider.create_calls), 2)

    def test_wire_inherits_manifest_ready_for_assets_without_status(self):
        manifest = self._manifest_for_root("/data/job-assets")
        manifest["materialization_status"] = "ready"
        for scene in manifest["scenes"]:
            for asset in scene["assets"]:
                asset.pop("status", None)
        provider = FakeProvider(manifest=manifest)

        result = wire_explicit_asset_hub_bundle(
            make_intent(),
            provider,
            {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
        )

        self.assertEqual(result["asset_hub"]["bundle_uid"], "jab_test")

    def test_materialization_not_ready_has_no_manifest_usable(self):
        provider = FakeProvider(materialize_response={"status": "pending"})

        with self.assertRaises(KurukinAssetHubMaterializationNotReady):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

        self.assertEqual(provider.manifest_calls, [])

    def test_ready_contract_strict_materialization_status_ready(self):
        self.assertTrue(_is_ready_response({"materialization_status": "ready"}))

    def test_ready_contract_strict_status_ready(self):
        self.assertTrue(_is_ready_response({"status": "ready"}))

    def test_ready_contract_materialization_status_overrides_status(self):
        self.assertFalse(
            _is_ready_response({"materialization_status": "failed", "status": "ready"})
        )
        self.assertFalse(
            _is_ready_response({"materialization_status": "partial", "status": "ready"})
        )

    def test_ready_contract_rejects_completed_and_ready_bool(self):
        self.assertFalse(_is_ready_response({"status": "completed"}))
        self.assertFalse(_is_ready_response({"ready": True}))

    def test_get_renderer_manifest_is_validated(self):
        provider = FakeProvider(manifest={"bundle_uid": "jab_test"})

        with self.assertRaises(ValueError):
            wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

    def test_resolve_renderer_manifest_path_default(self):
        self.assertEqual(
            resolve_renderer_manifest_path("jab_xxx"),
            "/data/job-assets/jab_xxx/manifests/renderer-manifest.json",
        )

    def test_resolve_renderer_manifest_path_uses_materialized_root_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "ASSET_HUB_MATERIALIZED_ROOT": "/tmp/custom-assets",
                "ASSET_HUB_JOB_ASSETS_DIR": "/tmp/legacy-assets",
            },
        ):
            result = resolve_renderer_manifest_path("jab_x")

        self.assertEqual(
            result,
            "/tmp/custom-assets/jab_x/manifests/renderer-manifest.json",
        )

    def test_resolve_renderer_manifest_path_empty_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["ASSET_HUB_MATERIALIZED_ROOT"] = ""
            result = resolve_renderer_manifest_path("jab_x")

        self.assertEqual(
            result,
            "/data/job-assets/jab_x/manifests/renderer-manifest.json",
        )

    def test_bundle_uid_parent_path_rejected(self):
        with self.assertRaises(ValueError):
            resolve_renderer_manifest_path("../bundle")

    def test_manifest_path_does_not_escape_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "job-assets"
            root.mkdir()

            result = resolve_renderer_manifest_path("jab_safe", root=root)

        self.assertTrue(result.endswith("/jab_safe/manifests/renderer-manifest.json"))
        self.assertIn("job-assets", result)

    def test_final_result_has_asset_hub_renderer_manifest_path(self):
        manifest = self._manifest_for_root("/data/job-assets")
        provider = FakeProvider(manifest=manifest)

        result = wire_explicit_asset_hub_bundle(
            make_intent(),
            provider,
            {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
        )

        self.assertEqual(
            result["asset_hub"]["renderer_manifest_path"],
            "/data/job-assets/jab_test/manifests/renderer-manifest.json",
        )

    def test_missing_job_id_does_not_create_bundle(self):
        manifest = self._manifest_for_root("/data/job-assets")
        provider = FakeProvider(manifest=manifest)
        intent = make_intent()
        intent.pop("task_id")
        intent.pop("job_id", None)

        with self.assertRaisesRegex(KurukinAssetHubWiringError, "job_id is required"):
            wire_explicit_asset_hub_bundle(
                intent,
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
            )

        self.assertEqual(provider.create_calls, [])

    def test_result_passes_through_build_moneyprinter_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "job-assets"
            for filename, scene in (("clip-a.mp4", "scene-001"), ("clip-b.mp4", "scene-002")):
                asset = root / "jab_test" / scene / filename
                asset.parent.mkdir(parents=True)
                asset.write_text("dummy", encoding="utf-8")
            manifest = self._manifest_for_root(root)
            provider = FakeProvider(manifest=manifest)
            result = wire_explicit_asset_hub_bundle(
                make_intent(),
                provider,
                {"scene-001": ["drive-a"], "scene-002": ["drive-b"]},
                root=root,
            )
            original_root = kurukin_job_adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
            kurukin_job_adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = str(root)
            try:
                payload = build_moneyprinter_payload(
                    {
                        "job_id": "asset-hub-wiring",
                        **result,
                        "video": {
                            "video_subject": "Asset Hub wiring",
                            "video_script": "Script.",
                            "video_aspect": "9:16",
                        },
                    },
                    media_probe=False,
                )
            finally:
                kurukin_job_adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = original_root

        self.assertEqual(payload["asset_hub_bundle_uid"], "jab_test")
        self.assertNotIn("video_materials", payload)

    def test_material_info_final_provider_asset_hub_reuses_manifest_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "job-assets"
            for filename, scene in (("clip-a.mp4", "scene-001"), ("clip-b.mp4", "scene-002")):
                asset = root / "jab_test" / scene / filename
                asset.parent.mkdir(parents=True)
                asset.write_text("dummy", encoding="utf-8")
            manifest = self._manifest_for_root(root)

            schema = types.ModuleType("app.models.schema")
            schema.MaterialInfo = SimpleNamespace
            with mock.patch.dict(sys.modules, {"app.models.schema": schema}), mock.patch.dict(
                os.environ,
                {"ASSET_HUB_JOB_ASSETS_DIR": str(root)},
            ):
                materials = convert_asset_hub_manifest_to_materials(manifest)

        self.assertEqual(materials[0].provider, "asset_hub")

    def test_search_does_not_need_drive_file_id(self):
        provider = FakeProvider(search_results=[[{"asset_uid": "drive-a", "filename": "a.mp4"}], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        self.assertEqual(result["asset_hub_selection"]["scenes"][0]["candidates"][0]["asset_uid"], "drive-a")

    def test_search_does_not_need_remote_path(self):
        provider = FakeProvider(search_results=[[{"asset_uid": "drive-a", "filename": "a.mp4"}], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        self.assertNotIn("remote_path", result["asset_hub_selection"]["scenes"][0]["candidates"][0])

    def test_search_does_not_need_rclone_remote(self):
        provider = FakeProvider(search_results=[[{"asset_uid": "drive-a", "filename": "a.mp4"}], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        self.assertNotIn("rclone_remote", result["asset_hub_selection"]["scenes"][0]["candidates"][0])

    def test_job_without_asset_hub_selection_keeps_existing_behavior(self):
        result = compile_job_intent_to_mpt_spec(
            {
                "mode": "audio_to_video",
                "topic": "Casa usada",
                "audio_path": "storage/local_audios/audio.mp3",
                "video_path": "storage/local_videos/visual.mp4",
            }
        )

        self.assertEqual(result["mpt_spec"]["mpt_params"]["video_source"], "local")
        self.assertEqual(result["mpt_spec"]["mpt_params"]["video_materials"][0]["url"], "storage/local_videos/visual.mp4")

    def test_no_automatic_external_stock_fallback(self):
        provider = FakeProvider(search_results=[[], []])

        result = search_asset_hub_candidates(
            provider,
            build_asset_hub_search_requests(make_intent(), generic_policy()),
        )

        self.assertEqual(result["status"], STATUS_NEEDS_INPUT)
        self.assertFalse(result["candidates_available"])
        serialized = json.dumps(result)
        self.assertNotIn("pexels", serialized)
        self.assertNotIn("pixabay", serialized)
        self.assertNotIn("coverr", serialized)

    def _manifest_for_root(self, root) -> dict:
        root_path = Path(root)
        return {
            "manifest_version": "1.0",
            "generated_by": "kurukin-asset-hub",
            "bundle_uid": "jab_test",
            "job_id": "mpt-001",
            "scenes": [
                {
                    "scene_id": "scene-001",
                    "scene_index": 1,
                    "assets": [
                        {
                            "asset_uid": "drive-a",
                            "status": "ready",
                            "type": "video",
                            "filename": "clip-a.mp4",
                            "local_path": str(root_path / "jab_test" / "scene-001" / "clip-a.mp4"),
                            "duration_seconds": 5,
                        }
                    ],
                },
                {
                    "scene_id": "scene-002",
                    "scene_index": 2,
                    "assets": [
                        {
                            "asset_uid": "drive-b",
                            "status": "ready",
                            "type": "video",
                            "filename": "clip-b.mp4",
                            "local_path": str(root_path / "jab_test" / "scene-002" / "clip-b.mp4"),
                            "duration_seconds": 5,
                        }
                    ],
                }
            ],
        }

    def _single_scene_manifest_with_statuses(
        self,
        *,
        manifest_statuses: dict | None = None,
        scene_statuses: dict | None = None,
        asset_statuses: dict | None = None,
    ) -> dict:
        manifest = {
            "manifest_version": "1.0",
            "generated_by": "kurukin-asset-hub",
            "bundle_uid": "jab_test",
            "job_id": "mpt-001",
            "scenes": [
                {
                    "scene_id": "scene-001",
                    "scene_index": 1,
                    "assets": [
                        {
                            "asset_uid": "drive-882918f4",
                            "type": "video",
                            "filename": "clip-a.mp4",
                            "local_path": "/data/job-assets/jab_test/scene-001/clip-a.mp4",
                        }
                    ],
                }
            ],
        }
        manifest.update(manifest_statuses or {})
        manifest["scenes"][0].update(scene_statuses or {})
        manifest["scenes"][0]["assets"][0].update(asset_statuses or {})
        return manifest


if __name__ == "__main__":
    unittest.main()
