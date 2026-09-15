import hashlib
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from app.custom.atlas_materializer import AtlasMaterializationError, AtlasMaterializer


class StreamResponse:
    def __init__(self, chunks, *, length=None, etag=None, content_type="video/mp4", status=200):
        self.status_code = status
        self.is_redirect = False
        self.headers = {"Content-Type": content_type, "ETag": etag or '"opaque"'}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.chunks = chunks
        self.closed = False

    @property
    def content(self):
        raise AssertionError("response.content must not be read")

    def iter_content(self, *, chunk_size):
        self.chunk_size = chunk_size
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.content_calls = []
        self.thumbnail_calls = []

    def open_content(self, uid, kind):
        self.content_calls.append((uid, kind))
        return self.response

    def open_thumbnail(self, uid, kind):
        self.thumbnail_calls.append((uid, kind))
        return self.response


class TestAtlasMaterializer(unittest.TestCase):
    def test_streams_to_deterministic_filename_and_reconstructs_identity(self):
        uid, data = str(uuid4()), b"hello atlas"
        response = StreamResponse([data[:3], data[3:]], length=len(data), etag=f'"{hashlib.sha256(data).hexdigest()}"')
        client = FakeClient(response)
        with tempfile.TemporaryDirectory() as tmp:
            output = AtlasMaterializer(client, chunk_size=3).materialize(uid, "horizontal", tmp)
            self.assertEqual(output.name, f"atlas-{uid}-horizontal.mp4")
            self.assertEqual(output.read_bytes(), data)
        self.assertEqual(client.content_calls, [(uid, "horizontal")])
        self.assertEqual(response.chunk_size, 3)
        self.assertTrue(response.closed)

    def test_bad_length_or_hash_removes_partial_file(self):
        uid = str(uuid4())
        length_data = b"short"
        cases = (
            StreamResponse([length_data], length=6, etag=f'"{hashlib.sha256(length_data).hexdigest()}"'),
            StreamResponse([b"wrong"], length=5, etag='"' + "0" * 64 + '"'),
        )
        for response in cases:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(AtlasMaterializationError):
                    AtlasMaterializer(FakeClient(response)).materialize(uid, "vertical", tmp)
                self.assertEqual(list(Path(tmp).iterdir()), [])
                self.assertTrue(response.closed)

    def test_invalid_headers_close_response_before_opening_temp_file(self):
        uid = str(uuid4())
        data = b"x"
        valid_etag = f'"{hashlib.sha256(data).hexdigest()}"'
        malformed_length = StreamResponse([data], etag=valid_etag)
        malformed_length.headers["Content-Length"] = "not-a-number"
        cases = (
            StreamResponse([data], content_type="text/plain", etag=valid_etag),
            malformed_length,
            StreamResponse([data], etag='"not-a-sha256"'),
        )
        for response in cases:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(AtlasMaterializationError):
                    AtlasMaterializer(FakeClient(response)).materialize(uid, "horizontal", tmp)
                self.assertTrue(response.closed)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_video_requires_a_quoted_64_hex_sha256_etag(self):
        uid = str(uuid4())
        invalid_etags = ("abc", '"abc"', 'W/"' + "0" * 64 + '"', '"' + "0" * 63 + '"', '"' + "0" * 65 + '"')
        for etag in invalid_etags:
            response = StreamResponse([], length=0, etag=etag)
            with self.subTest(etag=etag), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(AtlasMaterializationError):
                    AtlasMaterializer(FakeClient(response)).materialize(uid, "horizontal", tmp)
                self.assertTrue(response.closed)
        missing = StreamResponse([], length=0, etag='"' + "0" * 64 + '"')
        del missing.headers["ETag"]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AtlasMaterializationError):
            AtlasMaterializer(FakeClient(missing)).materialize(uid, "horizontal", tmp)
        self.assertTrue(missing.closed)

    def test_thumbnail_is_safely_reconstructed_through_client(self):
        uid = str(uuid4())
        client = FakeClient(StreamResponse([]))
        self.assertIs(client.response, AtlasMaterializer(client).open_thumbnail(uid, "vertical"))
        self.assertEqual(client.thumbnail_calls, [(uid, "vertical")])
