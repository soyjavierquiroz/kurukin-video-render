import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_batch_intents import (
    build_audio_batch_intents,
    discover_audio_inputs,
    enqueue_audio_batch_intents,
)


class TestKurukinBatchIntents(unittest.TestCase):
    def _root_with_visual(self) -> tempfile.TemporaryDirectory:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        visual = root / "storage" / "local_videos" / "batch-vertical.mp4"
        visual.parent.mkdir(parents=True)
        visual.write_bytes(b"visual")
        return tmp

    def test_discovers_audios_from_list(self):
        result = discover_audio_inputs(
            audio_paths=[
                "storage/local_audios/one.mp3",
                "storage/local_audios/two.wav",
            ]
        )

        self.assertEqual(
            result,
            [
                "storage/local_audios/one.mp3",
                "storage/local_audios/two.wav",
            ],
        )

    def test_discovers_audios_from_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_dir = root / "storage" / "local_audios"
            audio_dir.mkdir(parents=True)
            (audio_dir / "one.mp3").write_bytes(b"audio")
            (audio_dir / "two.m4a").write_bytes(b"audio")

            result = discover_audio_inputs(
                audio_folder="storage/local_audios",
                project_root=root,
            )

        self.assertEqual(
            result,
            [
                "storage/local_audios/one.mp3",
                "storage/local_audios/two.m4a",
            ],
        )

    def test_ignores_invalid_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_dir = root / "storage" / "local_audios"
            audio_dir.mkdir(parents=True)
            (audio_dir / "one.mp3").write_bytes(b"audio")
            (audio_dir / "notes.txt").write_bytes(b"not audio")

            result = discover_audio_inputs(
                audio_folder="storage/local_audios",
                audio_paths=["storage/local_audios/clip.mov"],
                project_root=root,
            )

        self.assertEqual(result, ["storage/local_audios/one.mp3"])

    def test_rejects_urls(self):
        with self.assertRaises(ValueError):
            discover_audio_inputs(audio_paths=["https://example.test/audio.mp3"])

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            discover_audio_inputs(audio_paths=["../storage/local_audios/audio.mp3"])

    def test_respects_max_items(self):
        result = discover_audio_inputs(
            audio_paths=[
                "storage/local_audios/one.mp3",
                "storage/local_audios/two.mp3",
                "storage/local_audios/three.mp3",
            ],
            max_items=2,
        )

        self.assertEqual(len(result), 2)

    def test_builds_unique_task_ids(self):
        intents = build_audio_batch_intents(
            audio_paths=[
                "storage/local_audios/one.mp3",
                "storage/local_audios/one.mp3",
            ],
            task_id_prefix="batch smoke",
        )

        self.assertEqual(
            [intent["task_id"] for intent in intents],
            ["batch-smoke-001", "batch-smoke-002"],
        )

    def test_enqueue_batch_ready_to_submit(self):
        with self._root_with_visual() as tmp:
            root = Path(tmp)
            queue_dir = root / "pending"
            result = enqueue_audio_batch_intents(
                audio_paths=[
                    "storage/local_audios/one.mp3",
                    "storage/local_audios/two.wav",
                ],
                topic="Batch smoke",
                task_id_prefix="batch-ready",
                queue_dir=queue_dir,
                project_root=root,
            )
            payloads = [
                json.loads(Path(item["queue_item_path"]).read_text(encoding="utf-8"))
                for item in result["items"]
            ]

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(
            [item["task_id"] for item in result["items"]],
            ["batch-ready-001", "batch-ready-002"],
        )
        for item, payload in zip(result["items"], payloads):
            self.assertEqual(item["status"], "QUEUED")
            self.assertEqual(payload["source"], "job_intent_v1")
            self.assertIn("original_intent", payload)
            self.assertIn("normalized_intent", payload)
            self.assertIn("compiled_mpt_spec", payload)
            self.assertEqual(
                payload["resolved_visual_path"],
                "storage/local_videos/batch-vertical.mp4",
            )
            self.assertEqual(payload["visual_autofill_source"], "local_picker_v1")
            self.assertFalse(payload["guardrails"]["real_render_started"])

    def test_needs_input_item_is_skipped_without_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_dir = root / "pending"
            result = enqueue_audio_batch_intents(
                audio_paths=["storage/local_audios/one.mp3"],
                task_id_prefix="batch-skip",
                queue_dir=queue_dir,
                project_root=root,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["items"][0]["status"], "NEEDS_INPUT")
        self.assertIn("needs_local_visual_asset", result["items"][0]["reasons"])
        self.assertFalse(queue_dir.exists())

    def test_no_audio_inputs_returns_reason(self):
        result = enqueue_audio_batch_intents(audio_paths=["storage/local_audios/nope.txt"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_audio_inputs")

    def test_helper_does_not_call_network_or_external_providers(self):
        with self._root_with_visual() as tmp:
            root = Path(tmp)
            with mock.patch.object(socket, "create_connection") as create_connection:
                result = enqueue_audio_batch_intents(
                    audio_paths=["storage/local_audios/one.mp3"],
                    task_id_prefix="batch-network",
                    queue_dir=root / "pending",
                    project_root=root,
                )

        self.assertTrue(result["ok"], result)
        create_connection.assert_not_called()

    def test_source_does_not_reference_forbidden_execution_surfaces(self):
        source = Path("app/custom/kurukin_batch_intents.py").read_text(encoding="utf-8")
        forbidden = (
            "openai",
            "text_to_speech",
            "pexels",
            "pixabay",
            "coverr",
            "asset_hub",
            "requests.",
            "urlopen",
            "task.start",
            "/api/v1/videos",
            "run_controlled_runner",
            "nightly_runner",
        )
        for item in forbidden:
            self.assertNotIn(item, source.lower())


if __name__ == "__main__":
    unittest.main()
