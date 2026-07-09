import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.pexels_source import (
    build_pexels_video_search_url,
    create_pexels_downloader,
    download_pexels_video_files,
    get_pexels_api_key,
    search_pexels_videos,
    select_pexels_video_files,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self, size=-1):
        return self.payload if size is None or size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def fake_pexels_response():
    return {
        "videos": [
            {
                "id": 101,
                "url": "https://www.pexels.com/video/city-101/",
                "user": {
                    "name": "Ana Video",
                    "url": "https://www.pexels.com/@ana/",
                },
                "video_files": [
                    {
                        "file_type": "video/mp4",
                        "width": 1920,
                        "height": 1080,
                        "link": "https://videos.pexels.com/landscape.mp4",
                    },
                    {
                        "file_type": "video/mp4",
                        "width": 720,
                        "height": 1280,
                        "link": "https://videos.pexels.com/portrait.mp4",
                    },
                ],
            },
            {
                "id": 101,
                "url": "https://www.pexels.com/video/city-101-duplicate/",
                "user": {"name": "Duplicate", "url": "https://example.com"},
                "video_files": [
                    {
                        "file_type": "video/mp4",
                        "width": 720,
                        "height": 1280,
                        "link": "https://videos.pexels.com/duplicate-id.mp4",
                    }
                ],
            },
            {
                "id": 202,
                "url": "https://www.pexels.com/video/walk-202/",
                "user": {
                    "name": "Luis Clips",
                    "url": "https://www.pexels.com/@luis/",
                },
                "video_files": [
                    {
                        "file_type": "video/mp4",
                        "width": 540,
                        "height": 960,
                        "link": "https://videos.pexels.com/walk.mp4",
                    }
                ],
            },
        ]
    }


class TestPexelsSource(unittest.TestCase):
    def test_build_url_uses_video_search_endpoint(self):
        url = build_pexels_video_search_url(query="city walking")

        self.assertIn("https://api.pexels.com/v1/videos/search?", url)
        self.assertIn("query=city+walking", url)
        self.assertIn("orientation=portrait", url)

    def test_search_uses_direct_authorization_header(self):
        seen = {}

        def opener(req):
            seen["url"] = req.full_url
            seen["authorization"] = req.get_header("Authorization")
            return FakeResponse(json.dumps({"videos": []}).encode("utf-8"))

        search_pexels_videos(query="city", api_key="pexels-test-key", opener=opener)

        self.assertIn("/v1/videos/search", seen["url"])
        self.assertEqual(seen["authorization"], "pexels-test-key")
        self.assertNotIn("Bearer", seen["authorization"])

    def test_get_pexels_api_key_does_not_print_key(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            key = get_pexels_api_key({"PEXELS_API_KEY": "secret-value"})

        self.assertEqual(key, "secret-value")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_search_uses_fake_opener(self):
        calls = []

        def opener(req):
            calls.append(req.full_url)
            return FakeResponse(json.dumps(fake_pexels_response()).encode("utf-8"))

        response = search_pexels_videos(
            query="city",
            api_key="pexels-test-key",
            opener=opener,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(response["videos"][0]["id"], 101)

    def test_select_prefers_mp4_portrait(self):
        selected = select_pexels_video_files(fake_pexels_response(), desired_count=1)

        self.assertEqual(selected[0]["link"], "https://videos.pexels.com/portrait.mp4")
        self.assertEqual(selected[0]["width"], 720)
        self.assertEqual(selected[0]["height"], 1280)

    def test_select_dedupes_by_video_id_and_file_link(self):
        selected = select_pexels_video_files(fake_pexels_response(), desired_count=2)

        self.assertEqual(
            [item["link"] for item in selected],
            [
                "https://videos.pexels.com/portrait.mp4",
                "https://videos.pexels.com/walk.mp4",
            ],
        )

    def test_desired_count_must_be_between_one_and_eight(self):
        with self.assertRaisesRegex(ValueError, "desired_count must be between 1 and 8"):
            select_pexels_video_files(fake_pexels_response(), desired_count=0)

        with self.assertRaisesRegex(ValueError, "desired_count must be between 1 and 8"):
            select_pexels_video_files(fake_pexels_response(), desired_count=9)

    def test_download_uses_fake_opener_and_writes_under_tmp_storage(self):
        selected = select_pexels_video_files(fake_pexels_response(), desired_count=1)
        seen = []

        def opener(req):
            seen.append(req.full_url)
            return FakeResponse(b"small-video-bytes")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "storage" / "local_videos" / "pexels"
            downloaded = download_pexels_video_files(
                selected_files=selected,
                output_dir=output_dir,
                opener=opener,
                max_bytes_per_file=5,
            )
            payload = Path(downloaded[0]["path"]).read_bytes()

        self.assertEqual(seen, ["https://videos.pexels.com/portrait.mp4"])
        self.assertEqual(downloaded[0]["source_provider"], "pexels")
        self.assertTrue(downloaded[0]["path"].endswith(".mp4"))
        self.assertEqual(payload, b"small")

    def test_output_dir_outside_allowed_roots_fails(self):
        selected = select_pexels_video_files(fake_pexels_response(), desired_count=1)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                ValueError,
                "Pexels output_dir must be under an allowed local asset root",
            ):
                download_pexels_video_files(
                    selected_files=selected,
                    output_dir=Path(tmp) / "outside",
                    opener=lambda _req: FakeResponse(b"bytes"),
                )

    def test_metadata_includes_attribution_fields(self):
        selected = select_pexels_video_files(fake_pexels_response(), desired_count=1)

        with tempfile.TemporaryDirectory() as tmp:
            downloaded = download_pexels_video_files(
                selected_files=selected,
                output_dir=Path(tmp) / "storage" / "local_assets",
                opener=lambda _req: FakeResponse(b"bytes"),
            )

        metadata = downloaded[0]
        self.assertEqual(metadata["source_provider"], "pexels")
        self.assertEqual(metadata["pexels_video_id"], "101")
        self.assertEqual(metadata["photographer"], "Ana Video")
        self.assertEqual(metadata["photographer_url"], "https://www.pexels.com/@ana/")
        self.assertEqual(metadata["pexels_url"], "https://www.pexels.com/video/city-101/")

    def test_error_without_api_key(self):
        with self.assertRaisesRegex(ValueError, "Pexels API key is not configured"):
            search_pexels_videos(query="city", api_key="", opener=lambda _req: None)

        with self.assertRaisesRegex(ValueError, "Pexels API key is not configured"):
            create_pexels_downloader(env={})

    def test_create_downloader_uses_fake_opener_only(self):
        calls = []

        def opener(req):
            calls.append(req.full_url)
            if "/v1/videos/search" in req.full_url:
                return FakeResponse(json.dumps(fake_pexels_response()).encode("utf-8"))
            return FakeResponse(b"downloaded")

        downloader = create_pexels_downloader(
            api_key="pexels-test-key",
            opener=opener,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = downloader(
                {
                    "query": "city",
                    "needed_count": 1,
                    "output_dir": str(Path(tmp) / "storage" / "local_videos"),
                }
            )

        self.assertEqual(result["source_provider"], "pexels")
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("/v1/videos/search" in call for call in calls))

    def test_create_downloader_can_be_disabled_per_request(self):
        downloader = create_pexels_downloader(
            api_key="pexels-test-key",
            opener=lambda _req: FakeResponse(b"{}"),
        )

        with self.assertRaisesRegex(ValueError, "Pexels source is disabled"):
            downloader({"pexels_enabled": False, "needed_count": 1})


if __name__ == "__main__":
    unittest.main()
