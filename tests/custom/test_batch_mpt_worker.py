import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from app.custom.material_source_policy import PROVIDER_ATLAS
from app import services
from scripts import batch_mpt_worker


class TestTaskLocalCustomAudio(unittest.TestCase):
    def test_copies_canonical_audio_into_task_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "content-job" / "source.mp3"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"trusted-audio")
            task_dir = root / "tasks" / "task-001"

            materialized = batch_mpt_worker._task_local_custom_audio(
                {"audio_file": str(source), "task_dir": str(task_dir)}
            )

            destination = Path(materialized)
            self.assertEqual(destination, task_dir / "custom-audio.mp3")
            self.assertEqual(destination.read_bytes(), b"trusted-audio")

    def test_atlas_title_identity_alone_does_not_enable_atlas(self):
        policy = batch_mpt_worker._material_source_policy({
            "atlas_title_id": "romper-el-circulo",
        })
        self.assertNotIn(PROVIDER_ATLAS, policy["providers"]["enabled"])

    def test_explicit_atlas_policy_is_preserved_without_asset_hub_scopes(self):
        policy = batch_mpt_worker._material_source_policy({
            "material_source_policy": {
                "providers": {"enabled": [PROVIDER_ATLAS]},
                "asset_hub": {"include": {}, "exclude": {}},
            },
            "atlas_title_id": "romper-el-circulo",
        })
        self.assertEqual(policy["providers"]["enabled"], (PROVIDER_ATLAS,))
        self.assertEqual(policy["asset_hub"]["include"], {
            "generic": False, "brands": (), "titles": (),
            "all_brands": False, "all_titles": False,
        })

    def test_review_task_receives_explicit_policy_and_atlas_title_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp3"
            source.write_bytes(b"trusted-audio")
            plan = root / "review-plan.json"
            manifest = {
                "batch_id": "batch",
                "task_id": "task",
                "task_dir": str(root / "task"),
                "stem": "display-subject-not-atlas-identity",
                "script": "script",
                "audio_file": str(source),
                "text_file": str(root / "script.txt"),
                "production_plan_path": str(plan),
                "material_source_policy": {
                    "providers": {"enabled": [PROVIDER_ATLAS]},
                    "asset_hub": {"include": {}, "exclude": {}},
                },
                "atlas_title_id": "romper-el-circulo",
            }

            def start(_task_id, params, *, stop_at):
                self.assertEqual(stop_at, "review")
                self.assertEqual(params.material_source_policy["providers"]["enabled"], (PROVIDER_ATLAS,))
                self.assertEqual(params.atlas_title_id, "romper-el-circulo")
                plan.write_text("{}", encoding="utf-8")

            with patch.object(
                services, "task", SimpleNamespace(start=start), create=True,
            ):
                batch_mpt_worker.run_review(manifest)
