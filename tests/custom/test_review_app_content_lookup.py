import unittest

from scripts.review_app import (
    alternative_authorized_elsewhere,
    plan_content_id,
    should_enqueue_nightly,
)


class ReviewAppContentLookupTests(unittest.TestCase):
    def test_legacy_content_job_metadata_resolves_content_id(self):
        plan = {
            "content_job": {
                "content_id": "cf_legacy_001",
            },
            "batch_id": "legacy-batch",
        }

        self.assertEqual(
            plan_content_id(plan),
            "cf_legacy_001",
        )
        self.assertFalse(
            should_enqueue_nightly(plan),
        )

    def test_current_automation_plan_resolves_batch_id_as_content_id(self):
        plan = {
            "batch_id": "cf_000002",
            "task_id": (
                "batch-content-constelaciones-familiares-"
                "cf_000002-La-mujer-que-pide-perd-n-por-todo"
            ),
            "audio_path": (
                "/MoneyPrinterTurbo/storage/content_jobs/"
                "constelaciones-familiares/cf_000002/source.mp3"
            ),
        }

        self.assertEqual(
            plan_content_id(plan),
            "cf_000002",
        )
        self.assertFalse(
            should_enqueue_nightly(plan),
        )

    def test_content_job_path_is_valid_secondary_signal(self):
        plan = {
            "batch_id": "cf_000003",
            "audio_path": (
                "/MoneyPrinterTurbo/storage/content_jobs/"
                "constelaciones-familiares/cf_000003/source.mp3"
            ),
        }

        self.assertEqual(
            plan_content_id(plan),
            "cf_000003",
        )
        self.assertFalse(
            should_enqueue_nightly(plan),
        )

    def test_legacy_nightly_plan_keeps_legacy_enqueue_behavior(self):
        plan = {
            "batch_id": "noche-mi-otra-yo-2026-08-20",
            "task_id": (
                "batch-noche-mi-otra-yo-2026-08-20-"
                "La-mujer-que-no-sabe-recibir"
            ),
            "audio_path": "/MoneyPrinterTurbo/storage/tasks/source.mp3",
        }

        self.assertIsNone(
            plan_content_id(plan),
        )
        self.assertTrue(
            should_enqueue_nightly(plan),
        )

    def test_blocked_alternative_has_no_primary_action_location(self):
        plan = {
            "segments": [
                {"segment_id": "segment-001", "selected_asset": {"asset_uid": "asset-a"}},
                {"segment_id": "segment-002", "selected_asset": {"asset_uid": "asset-b"}},
            ],
        }
        self.assertEqual(
            alternative_authorized_elsewhere(plan, "segment-001", "asset-b"),
            "segment-002",
        )
        self.assertIsNone(
            alternative_authorized_elsewhere(plan, "segment-001", "asset-c"),
        )


if __name__ == "__main__":
    unittest.main()
