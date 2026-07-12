import inspect
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_topic_planner import (
    TOPIC_PLAN_REASON_NEEDS_AUDIO,
    TOPIC_PLAN_STATUS_NEEDS_AUDIO,
    build_topic_script_plan,
    extract_visual_keywords,
    generate_local_script,
    split_script_into_scenes,
)


class TestKurukinTopicPlanner(unittest.TestCase):
    def test_topic_to_video_generates_local_script(self):
        plan = build_topic_script_plan(
            {
                "mode": "topic_to_video",
                "topic": "5 errores al comprar una casa usada",
                "preset": "educational",
                "duration_seconds": 45,
                "language": "es",
            }
        )

        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["status"], TOPIC_PLAN_STATUS_NEEDS_AUDIO)
        self.assertEqual(plan["reason"], TOPIC_PLAN_REASON_NEEDS_AUDIO)
        self.assertIn("5 errores al comprar una casa usada", plan["script"])
        self.assertEqual(plan["next_step"], "provide_audio_or_enable_tts")

    def test_topic_to_video_generates_scenes(self):
        plan = build_topic_script_plan(
            {
                "mode": "topic_to_video",
                "topic": "Errores al comprar una casa usada",
                "duration_seconds": 45,
            }
        )

        self.assertGreaterEqual(len(plan["scenes"]), 3)
        self.assertEqual(plan["scenes"][0]["index"], 1)
        self.assertGreater(plan["scenes"][0]["duration_seconds"], 0)
        self.assertTrue(plan["scenes"][0]["text"])
        self.assertTrue(plan["scenes"][0]["visual_keywords"])

    def test_topic_to_video_generates_visual_keywords(self):
        keywords = extract_visual_keywords(
            "5 errores al comprar una casa usada",
            "Revisa documentos, humedad y precio antes de decidir.",
            "educational",
        )

        self.assertIn("5 errores al comprar una casa usada", keywords)
        self.assertIn("checklist", keywords)
        self.assertGreaterEqual(len(keywords), 4)

    def test_split_script_into_scenes_distributes_duration(self):
        scenes = split_script_into_scenes("Uno.\nDos.\nTres.", 10)

        self.assertEqual(len(scenes), 3)
        self.assertEqual(sum(scene["duration_seconds"] for scene in scenes), 10)

    def test_supported_presets_produce_non_empty_scripts(self):
        for preset in (
            "educational",
            "listicle",
            "problem_solution",
            "sales",
            "story",
        ):
            with self.subTest(preset=preset):
                script = generate_local_script(
                    "comprar una casa usada",
                    preset,
                    45,
                    "es",
                )
                self.assertTrue(script.strip())
                self.assertIn("casa usada", script)

    def test_planner_does_not_call_network(self):
        with mock.patch.object(socket, "create_connection") as create_connection:
            plan = build_topic_script_plan(
                {
                    "mode": "topic_to_video",
                    "topic": "comprar una casa usada",
                }
            )

        self.assertTrue(plan["ok"])
        create_connection.assert_not_called()

    def test_planner_source_has_no_external_provider_calls(self):
        import app.custom.kurukin_topic_planner as planner

        source = inspect.getsource(planner).lower()
        for forbidden in (
            "openai",
            "elevenlabs",
            "voice.create",
            "text_to_speech",
            "pexels",
            "pixabay",
            "coverr",
            "asset_hub",
            "requests.",
            "urlopen",
            "/api/v1/videos",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
