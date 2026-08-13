import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.custom.material_discovery import MaterialCandidate
from app.custom.material_acquisition import MaterialAcquisitionUnavailable, acquire_selected_materials
from app.models.schema import MaterialInfo
from app.services import material as material_service

download_material_candidate = getattr(material_service, "download_material_candidate", None)


def decision(candidate): return SimpleNamespace(candidate=candidate)


class TestMaterialAcquisition(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()
    def storage(self, sub="", create=False): return str(Path(self.tmp.name) / sub)

    def test_stock_downloads_to_task_dir_and_manifest_is_safe(self):
        candidate = MaterialCandidate("pexels", "pexels:1", "pexels:1", "cat", url="https://download/1", source_info={"token": "bad"})
        with patch("app.custom.material_acquisition.utils.storage_dir", self.storage), patch("app.custom.material_acquisition.material.download_material_candidate", return_value=str(Path(self.tmp.name) / "tasks/t1/materials/a.mp4"), create=True) as download:
            result = acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(candidate),)), task_id="t1")
        self.assertIn("tasks/t1/materials", download.call_args.args[1])
        self.assertEqual(result.materials[0].provider, "pexels")
        payload = json.loads(Path(result.manifest_path).read_text())
        self.assertNotIn("token", str(payload).lower())
        self.assertEqual(payload["selected"][0]["local_path"], "materials/a.mp4")

    def test_mixed_uses_asset_hub_wiring_without_copying(self):
        hub = MaterialCandidate("asset_hub", "uid-a", "hub:a", "cat")
        stock = MaterialCandidate("pexels", "pexels:1", "p:1", "dog", url="https://download/1")
        shared = "/data/job-assets/bundle/a.mp4"
        with patch("app.custom.material_acquisition.utils.storage_dir", self.storage), patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", return_value={"asset_hub": {"bundle_uid": "bundle"}}) as wire, patch("app.custom.material_acquisition.convert_asset_hub_manifest_to_materials", return_value=[MaterialInfo(provider="asset_hub", url=shared)]), patch("app.custom.material_acquisition.material.download_material_candidate", return_value=str(Path(self.tmp.name) / "tasks/t1/materials/p.mp4"), create=True):
            provider = SimpleNamespace(get_renderer_manifest=lambda uid: {"bundle_uid": uid})
            result = acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(hub), decision(stock))), task_id="t1", asset_hub_provider=provider)
        self.assertEqual([x.provider for x in result.materials], ["asset_hub", "pexels"])
        self.assertEqual(result.materials[0].url, shared)
        self.assertTrue(wire.called)

    def test_503_and_traversal_are_clear(self):
        hub = MaterialCandidate("asset_hub", "uid-a", "hub:a", "cat")
        error = RuntimeError("busy"); error.status_code = 503
        with patch("app.custom.material_acquisition.utils.storage_dir", self.storage), patch("app.custom.material_acquisition.wire_explicit_asset_hub_bundle", side_effect=error):
            with self.assertRaises(MaterialAcquisitionUnavailable): acquire_selected_materials(selection_result=SimpleNamespace(decisions=(decision(hub),)), task_id="t1", asset_hub_provider=object())
        with self.assertRaises(ValueError): acquire_selected_materials(selection_result=SimpleNamespace(decisions=()), task_id="../bad")

    def test_stock_download_records_existing_external_history(self):
        if download_material_candidate is None:
            self.skipTest("material service is replaced by an isolated test fixture")
        candidate = MaterialCandidate("pexels", "pexels:1", "pexels:1", "cat", url="https://download/1")
        with patch.object(material_service, "save_video", return_value="/tmp/a.mp4", create=True), patch.object(material_service, "record_external_asset_usage") as record:
            self.assertEqual(download_material_candidate(candidate, "/tmp"), "/tmp/a.mp4")
        record.assert_called_once()


if __name__ == "__main__": unittest.main()
