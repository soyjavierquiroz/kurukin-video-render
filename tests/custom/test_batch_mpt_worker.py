import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from app.custom.material_source_policy import PROVIDER_ATLAS
from app import services
from app.models.schema import MaterialInfo
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


class TestApprovedMaterializationIntegrity(unittest.TestCase):
    asset_uid = "123e4567-e89b-12d3-a456-426614174000"

    def atlas_selection(self, *, asset_uid=None, rendition_kind="vertical"):
        uid = asset_uid or self.asset_uid
        return SimpleNamespace(decisions=(
            SimpleNamespace(candidate=SimpleNamespace(
                provider="atlas",
                canonical_id=f"{uid}:{rendition_kind}",
            )),
        ))

    def atlas_acquisition(self, *, asset_uid=None, rendition_kind="vertical"):
        uid = asset_uid or self.asset_uid
        return SimpleNamespace(materials=(
            MaterialInfo(
                provider="atlas",
                url="/tmp/atlas.mp4",
                duration=5,
                source_info={"asset_uid": uid, "rendition_kind": rendition_kind},
            ),
        ))

    def test_atlas_same_asset_and_rendition_passes(self):
        batch_mpt_worker._validate_approved_materialization(
            self.atlas_selection(), self.atlas_acquisition(),
        )

    def test_atlas_same_asset_different_rendition_fails(self):
        with self.assertRaisesRegex(
            RuntimeError,
            rf"unapproved asset_uids={self.asset_uid}:horizontal.*"
            rf"missing approved asset_uids={self.asset_uid}:vertical",
        ):
            batch_mpt_worker._validate_approved_materialization(
                self.atlas_selection(), self.atlas_acquisition(rendition_kind="horizontal"),
            )

    def test_atlas_different_asset_fails(self):
        other_uid = "123e4567-e89b-12d3-a456-426614174001"
        with self.assertRaisesRegex(RuntimeError, rf"unapproved asset_uids={other_uid}:vertical"):
            batch_mpt_worker._validate_approved_materialization(
                self.atlas_selection(), self.atlas_acquisition(asset_uid=other_uid),
            )

    def test_atlas_missing_approved_asset_fails(self):
        with self.assertRaisesRegex(RuntimeError, rf"missing approved asset_uids={self.asset_uid}:vertical"):
            batch_mpt_worker._validate_approved_materialization(
                self.atlas_selection(), SimpleNamespace(materials=()),
            )

    def test_atlas_extra_unapproved_asset_fails(self):
        other_uid = "123e4567-e89b-12d3-a456-426614174001"
        acquisition = SimpleNamespace(materials=(
            *self.atlas_acquisition().materials,
            self.atlas_acquisition(asset_uid=other_uid).materials[0],
        ))
        with self.assertRaisesRegex(RuntimeError, rf"unapproved asset_uids={other_uid}:vertical"):
            batch_mpt_worker._validate_approved_materialization(
                self.atlas_selection(), acquisition,
            )

    def test_non_atlas_identity_semantics_are_unchanged(self):
        selection = SimpleNamespace(decisions=(
            SimpleNamespace(candidate=SimpleNamespace(provider="pexels", canonical_id="pexels:1")),
        ))
        acquisition = SimpleNamespace(materials=(
            MaterialInfo(
                provider="pexels",
                url="/tmp/pexels.mp4",
                duration=5,
                source_info={"asset_id": "pexels:1"},
            ),
        ))
        batch_mpt_worker._validate_approved_materialization(selection, acquisition)
