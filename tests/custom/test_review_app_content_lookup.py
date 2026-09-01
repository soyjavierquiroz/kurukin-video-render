import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.custom import human_review
with patch.dict(sys.modules, {"streamlit": SimpleNamespace()}):
    from scripts import review_app
alternative_authorized_elsewhere = review_app.alternative_authorized_elsewhere
is_actionable_review = review_app.is_actionable_review
plan_content_id = review_app.plan_content_id
review_public_state = review_app.review_public_state
should_enqueue_nightly = review_app.should_enqueue_nightly


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


class _Column:
    def __init__(self, streamlit):
        self.streamlit = streamlit

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def metric(self, *_args, **_kwargs):
        pass

    def write(self, *_args, **_kwargs):
        pass

    def caption(self, value, *_args, **_kwargs):
        self.streamlit.captions.append(value)

    def button(self, label, **_kwargs):
        self.streamlit.buttons.append(label)
        return False


class _Streamlit:
    def __init__(self, content_id):
        self.content_id = content_id
        self.buttons = []
        self.errors = []
        self.infos = []
        self.captions = []
        self.sidebar = SimpleNamespace(radio=lambda _label, values, **_kwargs: values[0])

    @property
    def query_params(self):
        return {"content_id": self.content_id}

    def set_page_config(self, **_kwargs): pass
    def title(self, *_args): pass
    def subheader(self, *_args): pass
    def markdown(self, *_args): pass
    def divider(self): pass
    def write(self, *_args): pass
    def warning(self, *_args): pass
    def success(self, *_args): pass
    def rerun(self): pass
    def error(self, value): self.errors.append(value)
    def info(self, value): self.infos.append(value)
    def caption(self, value): self.captions.append(value)
    def columns(self, count):
        return [_Column(self) for _ in range(count if isinstance(count, int) else len(count))]
    def button(self, label, **_kwargs):
        self.buttons.append(label)
        return False
    def checkbox(self, _label, **_kwargs): return False


class ReviewAppReadinessGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.job_root = self.root / "storage" / "content_jobs"
        self.job_root_patcher = patch.object(
            review_app.content_ingest, "DEFAULT_JOB_ROOT", self.job_root,
        )
        self.job_root_patcher.start()
        self.plan_file = self.root / "storage" / "review_queue" / "cid" / "story" / "production-plan.json"
        self.plan_file.parent.mkdir(parents=True)
        self.plan = {
            "review_status": human_review.STATUS_PENDING,
            "batch_id": "cid",
            "stem": "story",
            "content_job": {"content_id": "cid", "niche_id": "niche"},
            "segments": [],
        }

    def tearDown(self):
        self.job_root_patcher.stop()
        self.tmp.cleanup()

    def _write_state(self, state, message=None):
        path = self.job_root / "niche" / "cid" / "review-preparation.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "content_id": "cid", "niche_id": "niche", "state": state,
            "last_error_message": message,
        }), encoding="utf-8")

    def _run_main(self):
        fake = _Streamlit("cid")
        with patch.object(review_app, "PROJECT_ROOT", self.root), \
             patch.object(review_app.content_ingest, "DEFAULT_JOB_ROOT", self.job_root), \
             patch.object(review_app, "st", fake):
            review_app.main()
        return fake

    def test_error_plan_is_non_actionable_and_shows_safe_message(self):
        self.plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        self._write_state("error", "Exclusive asset source is unavailable; no review material could be prepared.")
        state, message = review_public_state(self.plan)
        self.assertEqual(state, "ERROR")
        self.assertIn("Exclusive asset source is unavailable", message)
        self.assertFalse(is_actionable_review(self.plan))
        ui = self._run_main()
        self.assertEqual(ui.errors, ["Review is unavailable because preparation failed."])
        self.assertTrue(any("Exclusive asset source is unavailable" in item for item in ui.captions))
        self.assertEqual(ui.buttons, [])

    def test_preparing_plan_has_no_approval_controls(self):
        self.plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        self._write_state("running")
        self.assertEqual(review_public_state(self.plan)[0], "PREPARING_REVIEW")
        self.assertFalse(is_actionable_review(self.plan))
        ui = self._run_main()
        self.assertTrue(ui.infos)
        self.assertEqual(ui.buttons, [])

    def test_ready_plan_keeps_actionable_review_ui(self):
        self.plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        self._write_state("completed")
        self.assertEqual(review_public_state(self.plan)[0], "HUMAN_REVIEW_READY")
        self.assertTrue(is_actionable_review(self.plan))
        ui = self._run_main()
        self.assertIn("APPROVE JOB", ui.buttons)
        self.assertIn("REJECT JOB", ui.buttons)

    def test_approved_plan_remains_non_actionable(self):
        self.plan["review_status"] = human_review.STATUS_APPROVED
        self.plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        self._write_state("completed")
        self.assertFalse(is_actionable_review(self.plan))
        ui = self._run_main()
        self.assertEqual(ui.buttons, [])
        self.assertTrue(ui.errors)


if __name__ == "__main__":
    unittest.main()
