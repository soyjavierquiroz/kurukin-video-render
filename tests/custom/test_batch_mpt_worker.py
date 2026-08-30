import tempfile
from pathlib import Path
import unittest

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
