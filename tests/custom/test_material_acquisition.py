import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.custom.material_discovery import MaterialCandidate
from app.custom.material_acquisition import MaterialAcquisitionError, MaterialAcquisitionUnavailable, acquire_selected_materials
from app.custom.kurukin_asset_hub_wiring import KurukinAssetHubMaterializationNotReady
from app.models.schema import MaterialInfo
from app.services import material as material_service

download_material_candidate = getattr(material_service, "download_material_candidate", None)


def decision(candidate): return SimpleNamespace(candidate=candidate)


class TestMaterialAcquisition(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()
    def storage(self, sub="", create=False): return str(Path(self.tmp.name) / sub)

    def approved_plan(self, asset_uids):
        return {
            "review_status": "approved",
            "duration": len(asset_uids) * 5,
            "segments": [
                {
                    "segment_id": f"segment-{index:03d}",
                    "duration": 5,
                    "script_text": f"Approved narration {index}",
                    "selected_asset": {
                        "asset_uid": asset_uid,
                        "provider": "asset_hub",
                        "metadata": {"duration": 5},
                    },
                    "backup_assets": [],
                }
                for index, asset_uid in enumerate(asset_uids, 1)
            ],
        }

    def test_stock_downloads_to_task_dir_and_manifest_is_safe(self):
        candidate = MaterialCandidate("pexels", "pexels:1", "pexels:1", "cat", url="https://download/1", source_info={"token": "bad"})
        with patch("app.custom.material_acquisition.utils.storage_dir", self.storage), \
             patch("app.custom.material_acquisition.KurukinAssetProvider") as hub, \
             patch("app.custom.material_acquisition.material.download_material_candidate", return_value=str(Path(self.tmp.name) / "tasks/t1/materials/a.mp4"), create=True) as download:
            result = acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(candidate),)), task_id="t1")
        self.assertIn("tasks/t1/materials", download.call_args.args[1])
        hub.assert_not_called()
        self.assertEqual(result.materials[0].provider, "pexels")
        payload = json.loads(Path(result.manifest_path).read_text())
        self.assertNotIn("token", str(payload).lower())
        self.assertEqual(payload["selected"][0]["local_path"], "materials/a.mp4")

    def test_mixed_uses_asset_hub_wiring_without_copying(self):
        hub = MaterialCandidate("asset_hub", "uid-a", "hub:a", "cat")
        stock = MaterialCandidate("pexels", "pexels:1", "p:1", "dog", url="https://download/1")
        shared = Path(self.tmp.name) / "bundle/a.mp4"
        shared.parent.mkdir()
        shared.write_text("video")
        manifest = {
            "manifest_version": "1.0",
            "generated_by": "kurukin-asset-hub",
            "bundle_uid": "bundle",
            "status": "ready",
            "scenes": [
                {
                    "scene_id": "scene-001",
                    "scene_index": 1,
                    "assets": [
                        {"asset_uid": "uid-a", "type": "video", "filename": "a.mp4", "local_path": str(shared), "status": "ready"},
                    ],
                }
            ],
        }
        with patch.dict("os.environ", {"ASSET_HUB_MATERIALIZED_ROOT": self.tmp.name}), patch("app.custom.material_acquisition.utils.storage_dir", self.storage), patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", return_value={"asset_hub": {"bundle_uid": "bundle"}}) as wire, patch("app.custom.material_acquisition.material.download_material_candidate", return_value=str(Path(self.tmp.name) / "tasks/t1/materials/p.mp4"), create=True):
            provider = SimpleNamespace(get_renderer_manifest=lambda uid: manifest)
            result = acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(hub), decision(stock))), task_id="t1", asset_hub_provider=provider)
        self.assertEqual([x.provider for x in result.materials], ["asset_hub", "pexels"])
        self.assertEqual(result.materials[0].url, str(shared))
        self.assertTrue(wire.called)
        self.assertNotIn("brand_slug", wire.call_args.args[0])
        self.assertEqual(wire.call_args.kwargs["task_root"], Path(self.tmp.name) / "tasks")

    def test_asset_hub_manifest_extras_do_not_enter_materials(self):
        selected = MaterialCandidate("asset_hub", "uid-a", "hub:a", "cat")
        selected_path = Path(self.tmp.name) / "bundle/a.mp4"
        extra_path = Path(self.tmp.name) / "bundle/extra.mp4"
        selected_path.parent.mkdir()
        selected_path.write_text("selected")
        extra_path.write_text("extra")
        manifest = {
            "manifest_version": "1.0",
            "generated_by": "kurukin-asset-hub",
            "bundle_uid": "bundle",
            "status": "ready",
            "scenes": [
                {
                    "scene_id": "scene-001",
                    "scene_index": 1,
                    "assets": [
                        {"asset_uid": "uid-a", "type": "video", "filename": "a.mp4", "local_path": str(selected_path), "status": "ready"},
                        {"asset_uid": "uid-extra", "type": "video", "filename": "extra.mp4", "local_path": str(extra_path), "status": "ready"},
                    ],
                }
            ],
        }
        with patch.dict("os.environ", {"ASSET_HUB_MATERIALIZED_ROOT": self.tmp.name}), patch("app.custom.material_acquisition.utils.storage_dir", self.storage), patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", return_value={"asset_hub": {"bundle_uid": "bundle"}}):
            provider = SimpleNamespace(get_renderer_manifest=lambda uid: manifest)
            result = acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(selected),)), task_id="t1", asset_hub_provider=provider)
        self.assertEqual([item.url for item in result.materials], [str(selected_path)])
        payload = json.loads(Path(result.manifest_path).read_text())
        self.assertEqual([item["canonical_id"] for item in payload["selected"]], ["uid-a"])

    def test_approved_plan_assets_are_the_bundle_selection(self):
        selected = [MaterialCandidate("asset_hub", uid, f"hub:{uid}", "plan") for uid in ("A", "B", "C")]
        wire_result = {"asset_hub": {"bundle_uid": "bundle"}}
        bundle_dir = Path(self.tmp.name) / "bundle"
        bundle_dir.mkdir()
        assets = []
        for asset_uid in ("A", "B", "C"):
            path = bundle_dir / f"{asset_uid}.mp4"
            path.write_text("video")
            assets.append({"asset_uid": asset_uid, "type": "video", "filename": path.name, "local_path": str(path), "status": "ready"})
        manifest = {"manifest_version": "1.0", "generated_by": "kurukin-asset-hub", "bundle_uid": "bundle", "status": "ready", "scenes": [{"scene_id": "scene-001", "scene_index": 1, "assets": assets}]}
        with patch.dict("os.environ", {"ASSET_HUB_MATERIALIZED_ROOT": self.tmp.name}), patch("app.custom.material_acquisition.utils.storage_dir", self.storage), patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", return_value=wire_result) as wire:
            result = acquire_selected_materials(
                selection_result=SimpleNamespace(decisions=tuple(decision(item) for item in selected)),
                task_id="t1",
                asset_hub_provider=SimpleNamespace(get_renderer_manifest=lambda _uid: manifest),
                approved_plan=self.approved_plan(["A", "B", "C"]),
            )
        self.assertEqual([info.source_info["asset_id"] for info in result.materials], ["A", "B", "C"])
        self.assertEqual(
            [asset_uid for scene in wire.call_args.args[0]["scenes"] for asset_uid in scene["selected_asset_uids"]],
            ["A", "B", "C"],
        )
        self.assertEqual(
            [scene["scene_id"] for scene in wire.call_args.args[0]["scenes"]],
            ["segment-001", "segment-002", "segment-003"],
        )
        self.assertEqual(
            [scene["script_scene"] for scene in wire.call_args.args[0]["scenes"]],
            ["Approved narration 1", "Approved narration 2", "Approved narration 3"],
        )

    def test_approved_asset_hub_backups_stay_in_their_frozen_segment(self):
        selected = [MaterialCandidate("asset_hub", "A", "hub:A", "plan")]
        plan = self.approved_plan(["A", "B"])
        plan["segments"][0]["backup_assets"] = [{
            "asset_uid": "A-backup", "provider": "asset_hub", "metadata": {"duration": 5},
        }]
        bundle_dir = Path(self.tmp.name) / "bundle"
        bundle_dir.mkdir()
        assets = []
        for uid in ("A", "A-backup", "B"):
            path = bundle_dir / f"{uid}.mp4"
            path.write_text("video")
            assets.append({"asset_uid": uid, "type": "video", "filename": path.name,
                           "local_path": str(path), "status": "ready"})
        manifest = {"manifest_version": "1.0", "generated_by": "kurukin-asset-hub",
                    "bundle_uid": "bundle", "status": "ready",
                    "scenes": [{"scene_id": "unrelated", "assets": assets}]}
        with patch.dict("os.environ", {"ASSET_HUB_MATERIALIZED_ROOT": self.tmp.name}), \
             patch("app.custom.material_acquisition.utils.storage_dir", self.storage), \
             patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", return_value={"asset_hub": {"bundle_uid": "bundle"}}) as wire:
            acquire_selected_materials(
                selection_result=SimpleNamespace(decisions=(decision(selected[0]),)), task_id="t1",
                asset_hub_provider=SimpleNamespace(get_renderer_manifest=lambda _uid: manifest), approved_plan=plan,
            )
        self.assertEqual(wire.call_args.args[0]["scenes"], [
            {"scene_id": "segment-001", "scene_index": 1, "script_scene": "Approved narration 1", "selected_asset_uids": ["A", "A-backup"]},
            {"scene_id": "segment-002", "scene_index": 2, "script_scene": "Approved narration 2", "selected_asset_uids": ["B"]},
        ])

    def test_pixabay_does_not_use_asset_hub_or_require_approved_fields(self):
        pixabay = MaterialCandidate(
            "pixabay", "pixabay:1", "pixabay:1", "forest", url="https://download/1"
        )
        downloaded = Path(self.tmp.name) / "tasks/t1/materials/pixabay.mp4"
        with patch("app.custom.material_acquisition.utils.storage_dir", self.storage), \
             patch("app.custom.material_acquisition.KurukinAssetProvider") as hub, \
             patch("app.custom.material_acquisition.material.download_material_candidate", return_value=str(downloaded), create=True) as download:
            result = acquire_selected_materials(
                selection_result=SimpleNamespace(decisions=(decision(pixabay),)),
                task_id="t1",
            )
        hub.assert_not_called()
        download.assert_called_once()
        self.assertEqual(result.materials[0].provider, "pixabay")

    def test_approved_plan_bundle_drift_blocks_before_materialization(self):
        selected = MaterialCandidate("asset_hub", "B", "hub:B", "plan")
        with patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle") as wire:
            with self.assertRaisesRegex(MaterialAcquisitionError, "selected_asset_uids.*materialization blocked"):
                acquire_selected_materials(
                    selection_result=SimpleNamespace(decisions=(decision(selected),)),
                    task_id="t1",
                    asset_hub_provider=object(),
                    approved_plan=self.approved_plan(["A"]),
                )
        wire.assert_not_called()

    def test_approved_missing_asset_fails_closed_with_segment_and_uid(self):
        selected = MaterialCandidate("asset_hub", "A", "hub:A", "plan")
        manifest = {
            "manifest_version": "1.0", "generated_by": "kurukin-asset-hub",
            "bundle_uid": "bundle", "status": "ready", "scenes": [],
        }
        with patch.dict("os.environ", {"ASSET_HUB_MATERIALIZED_ROOT": self.tmp.name}), \
             patch("app.custom.material_acquisition.utils.storage_dir", self.storage), \
             patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", return_value={"asset_hub": {"bundle_uid": "bundle"}}):
            with self.assertRaisesRegex(
                MaterialAcquisitionError,
                "approved asset materialization failed: segment_id=segment-001 asset_uid=A",
            ):
                acquire_selected_materials(
                    selection_result=SimpleNamespace(decisions=(decision(selected),)), task_id="t1",
                    asset_hub_provider=SimpleNamespace(get_renderer_manifest=lambda _uid: manifest),
                    approved_plan=self.approved_plan(["A"]),
                )

    def test_503_and_traversal_are_clear(self):
        hub = MaterialCandidate("asset_hub", "uid-a", "hub:a", "cat")
        error = RuntimeError("busy"); error.status_code = 503
        with patch("app.custom.material_acquisition.utils.storage_dir", self.storage), patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", side_effect=error):
            with self.assertRaises(MaterialAcquisitionUnavailable): acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(hub),)), task_id="t1", asset_hub_provider=object())
        with self.assertRaises(ValueError): acquire_selected_materials(selection_result=SimpleNamespace(decisions=()), task_id="../bad")

    def test_approved_materialization_retries_exact_frozen_uids_and_third_success_continues(self):
        hub = MaterialCandidate("asset_hub", "uid-a", "hub:a", "cat")
        info = MaterialInfo(provider="asset_hub", url="/tmp/approved.mp4", duration=5)
        attempts = [KurukinAssetHubMaterializationNotReady("not ready"), KurukinAssetHubMaterializationNotReady("not ready"), ([info], "bundle")]
        with patch("app.custom.material_acquisition.utils.storage_dir", self.storage), \
             patch("app.custom.material_acquisition._asset_hub_materials", side_effect=attempts) as materialize, \
             patch("app.custom.material_acquisition.time.sleep") as sleep:
            result = acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(hub),)), task_id="t1", asset_hub_provider=object(), approved_plan=self.approved_plan(["uid-a"]))
        self.assertEqual(result.materials, (info,))
        self.assertEqual(sleep.call_args_list, [((5,),), ((15,),)])
        self.assertEqual(materialize.call_count, 3)
        self.assertEqual([[d.candidate.canonical_id for d in call.args[0]] for call in materialize.call_args_list], [["uid-a"]] * 3)
        self.assertEqual([call.kwargs["scene_ids"] for call in materialize.call_args_list], [["segment-001"]] * 3)

    def test_approved_materialization_exhaustion_blocks_and_open_sources_do_not_retry(self):
        hub = MaterialCandidate("asset_hub", "uid-a", "hub:a", "cat")
        not_ready = KurukinAssetHubMaterializationNotReady("not ready")
        with patch("app.custom.material_acquisition._asset_hub_materials", side_effect=[not_ready, not_ready, not_ready]) as materialize, \
             patch("app.custom.material_acquisition.time.sleep") as sleep:
            with self.assertRaises(KurukinAssetHubMaterializationNotReady):
                acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(hub),)), task_id="t1", asset_hub_provider=object(), approved_plan=self.approved_plan(["uid-a"]))
        self.assertEqual(materialize.call_count, 3)
        self.assertEqual(sleep.call_args_list, [((5,),), ((15,),)])

    def test_stock_download_records_existing_external_history(self):
        if download_material_candidate is None:
            self.skipTest("material service is replaced by an isolated test fixture")
        candidate = MaterialCandidate("pexels", "pexels:1", "pexels:1", "cat", url="https://download/1")
        with patch.object(material_service, "save_video", return_value="/tmp/a.mp4", create=True), patch.object(material_service, "record_external_asset_usage") as record:
            self.assertEqual(download_material_candidate(candidate, "/tmp"), "/tmp/a.mp4")
        record.assert_called_once()


if __name__ == "__main__": unittest.main()
