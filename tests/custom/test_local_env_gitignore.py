import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestLocalEnvGitignore(unittest.TestCase):
    def test_gitignore_covers_local_env_files(self):
        contents = Path(".gitignore").read_text(encoding="utf-8")

        self.assertIn("/.env", contents)
        self.assertIn("!/.env.example", contents)
        self.assertIn("*.local.env", contents)

    def test_env_example_uses_empty_placeholders_only(self):
        lines = Path(".env.example").read_text(encoding="utf-8").splitlines()
        key_lines = [
            line
            for line in lines
            if line and not line.startswith("#") and "=" in line
        ]

        self.assertGreaterEqual(len(key_lines), 4)
        for line in key_lines:
            key, value = line.split("=", 1)
            self.assertTrue(key)
            self.assertEqual(value, "")
