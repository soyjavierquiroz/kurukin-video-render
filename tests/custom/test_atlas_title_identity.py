import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.custom.atlas_provider import build_atlas_scope_from_params
from app.models.schema import TaskVideoRequest, VideoParams
from app.services import task_artifacts


class TestAtlasTitleIdentity(unittest.TestCase):
    def test_existing_video_params_remain_valid_without_an_atlas_identity(self):
        params = VideoParams(video_subject="An existing MPT job")

        self.assertIsNone(params.atlas_title_id)

    def test_explicit_identity_is_accepted_and_transport_whitespace_is_trimmed(self):
        params = VideoParams(
            video_subject="Display title only",
            atlas_title_id="  romper-el-circulo  ",
        )

        self.assertEqual(params.atlas_title_id, "romper-el-circulo")

    def test_api_request_contract_inherits_the_explicit_identity(self):
        request = TaskVideoRequest(
            video_subject="Display title only",
            atlas_title_id="romper-el-circulo",
        )

        self.assertEqual(request.atlas_title_id, "romper-el-circulo")

    def test_empty_identity_normalizes_to_none(self):
        self.assertIsNone(
            VideoParams(video_subject="Display title only", atlas_title_id=" \t\n ").atlas_title_id
        )

    def test_model_serialization_and_task_manifest_restore_preserve_identity(self):
        params = VideoParams(
            video_subject="A display title that is not an Atlas ID",
            atlas_title_id="romper-el-circulo",
        )
        serialized = params.model_dump(mode="json")
        self.assertEqual(serialized["atlas_title_id"], "romper-el-circulo")

        with tempfile.TemporaryDirectory() as temporary_directory:
            task_directory = Path(temporary_directory) / "identity-task"
            task_directory.mkdir()
            with patch.object(task_artifacts.utils, "task_dir", return_value=str(task_directory)):
                task_artifacts.write_script_data(
                    "identity-task", {"script": "script", "search_terms": [], "params": params}
                )

            manifest = json.loads((task_directory / "script.json").read_text(encoding="utf-8"))
            restored = VideoParams.model_validate(manifest["params"])

        self.assertEqual(restored.atlas_title_id, "romper-el-circulo")

    def test_scope_helper_returns_the_exact_explicit_title_scope(self):
        params = VideoParams(video_subject="Display title", atlas_title_id="romper-el-circulo")

        self.assertEqual(
            build_atlas_scope_from_params(params),
            {"kind": "title", "title_id": "romper-el-circulo"},
        )

    def test_scope_helper_never_derives_from_mpt_or_asset_hub_values(self):
        # These are intentionally values that could look title-like, yet none
        # is the dedicated Atlas namespace field.
        params = SimpleNamespace(
            video_subject="romper-el-circulo",
            task_id="romper-el-circulo",
            material_source_policy={
                "providers": {"enabled": ["asset_hub"]},
                "asset_hub": {"include": {"titles": ["romper-el-circulo"]}},
            },
            asset_hub_bundle_uid="romper-el-circulo",
        )

        self.assertIsNone(build_atlas_scope_from_params(params))


if __name__ == "__main__":
    unittest.main()
