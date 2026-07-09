import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestSourceProviderEnvWiring(unittest.TestCase):
    def test_docker_compose_webui_has_provider_env_passthrough(self):
        contents = Path("docker-compose.local.yml").read_text(encoding="utf-8")

        self.assertIn("PEXELS_API_KEY: ${PEXELS_API_KEY:-}", contents)
        self.assertIn("PEXELS_KEY: ${PEXELS_KEY:-}", contents)
        self.assertIn("PIXABAY_API_KEY: ${PIXABAY_API_KEY:-}", contents)
        self.assertIn("PIXABAY_KEY: ${PIXABAY_KEY:-}", contents)
        self.assertIn("COVERR_API_KEY: ${COVERR_API_KEY:-}", contents)
        self.assertIn("COVERR_KEY: ${COVERR_KEY:-}", contents)
        self.assertIn(
            "KURUKIN_ENABLE_PEXELS_SOURCE: ${KURUKIN_ENABLE_PEXELS_SOURCE:-}",
            contents,
        )

    def test_env_example_contains_only_placeholder_values(self):
        contents = Path(".env.example").read_text(encoding="utf-8")

        self.assertIn("PEXELS_API_KEY=", contents)
        self.assertIn("PIXABAY_API_KEY=", contents)
        self.assertIn("COVERR_API_KEY=", contents)
        self.assertNotIn("Bearer", contents)
