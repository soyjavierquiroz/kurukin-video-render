"""Import-boundary regression tests for the lightweight host dispatcher."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _fresh_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


class HostImportDependencyTests(unittest.TestCase):
    def test_lightweight_video_terms_preserves_native_parser_contract(self) -> None:
        from app.custom.video_terms import normalize_video_terms

        self.assertEqual(normalize_video_terms("worried woman, serious conversation， self blame"), [
            "worried woman", "serious conversation", "self blame",
        ])
        self.assertEqual(normalize_video_terms(" ,， "), ["", "", ""])
        self.assertEqual(normalize_video_terms([" one ", "", "two"]), ["one", "", "two"])

    def test_review_adapter_import_does_not_import_heavy_task_service(self) -> None:
        result = _fresh_python(
            "import sys; import scripts.create_content_job_review; "
            "assert 'app.services.task' not in sys.modules"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_host_runner_import_does_not_transitively_import_llm(self) -> None:
        result = _fresh_python(
            "import sys; import scripts.host_execution_runner; "
            "assert 'app.services.task' not in sys.modules; "
            "assert 'app.services.llm' not in sys.modules"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
