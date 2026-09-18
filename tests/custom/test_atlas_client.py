import unittest
from uuid import uuid4

from app.custom.atlas_client import (
    AtlasClient,
    AtlasInvalidRequestError,
    AtlasNotFoundError,
    AtlasProtocolError,
    AtlasServerError,
    AtlasUnavailableError,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, close_error=None):
        self.status_code = status_code
        self._body = body
        self.headers = {}
        self.closed = False
        self.close_error = close_error

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(("post", args, kwargs))
        return self.response

    def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return self.response


class TestAtlasClient(unittest.TestCase):
    def test_valid_base_url_is_normalized(self):
        client = AtlasClient("https://atlas.example/api///", session=FakeSession(FakeResponse()))
        self.assertEqual(client.base_url, "https://atlas.example/api")

    def test_invalid_base_urls_are_rejected(self):
        for value in ("", "ftp://atlas.example", "https://u:p@atlas.example", "https://atlas.example?q=1", "https://atlas.example/#x"):
            with self.subTest(value=value), self.assertRaises(AtlasInvalidRequestError):
                AtlasClient(value, session=FakeSession(FakeResponse()))

    def test_search_uses_exact_endpoint_and_returns_scarcity(self):
        session = FakeSession(FakeResponse(body={"schema_version": "asset_candidates_v1", "candidates": [], "scarcity_reason": "no_candidates_in_scope"}))
        result = AtlasClient("https://atlas.example/", timeout_seconds=3, session=session).search({"schema_version": "asset_search_v1"})
        self.assertEqual(result["scarcity_reason"], "no_candidates_in_scope")
        self.assertEqual(session.calls[0][1][0], "https://atlas.example/v1/assets/search")
        self.assertEqual(session.calls[0][2]["timeout"], 3.0)

    def test_delivery_reconstructs_exact_endpoint_and_disables_redirects(self):
        uid = str(uuid4())
        session = FakeSession(FakeResponse())
        client = AtlasClient("https://atlas.example", session=session)
        client.open_content(uid, "horizontal")
        client.open_thumbnail(uid, "vertical")
        self.assertEqual(session.calls[0][1][0], f"https://atlas.example/v1/assets/{uid}/renditions/horizontal/content")
        self.assertEqual(session.calls[1][1][0], f"https://atlas.example/v1/assets/{uid}/renditions/vertical/thumbnail")
        self.assertTrue(session.calls[0][2]["stream"])
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertFalse(session.response.closed)

    def test_public_thumbnail_url_requires_the_exact_logical_thumbnail_path(self):
        uid = str(uuid4())
        client = AtlasClient("https://atlas.example/api", session=FakeSession(FakeResponse()))
        locator = f"/v1/assets/{uid}/renditions/horizontal/thumbnail"
        self.assertEqual(
            client.public_thumbnail_url(uid, "horizontal", locator),
            f"https://atlas.example/api{locator}",
        )
        for unsafe in (
            f"/v1/assets/{uid}/renditions/horizontal/content",
            f"https://other.example{locator}",
            f"{locator}?query=1",
        ):
            with self.subTest(locator=unsafe), self.assertRaises(AtlasInvalidRequestError):
                client.public_thumbnail_url(uid, "horizontal", unsafe)

    def test_delivery_http_errors_close_streaming_responses(self):
        uid = str(uuid4())
        cases = ((404, AtlasNotFoundError), (503, AtlasUnavailableError))
        for status, error in cases:
            response = FakeResponse(status, {"message": "unavailable"})
            with self.subTest(status=status), self.assertRaises(error):
                AtlasClient("https://atlas.example", session=FakeSession(response)).open_content(uid, "horizontal")
            self.assertTrue(response.closed)

    def test_delivery_close_failure_does_not_obscure_atlas_http_error(self):
        response = FakeResponse(404, {"message": "missing"}, close_error=OSError("socket failed"))
        with self.assertRaises(AtlasNotFoundError):
            AtlasClient("https://atlas.example", session=FakeSession(response)).open_content(str(uuid4()), "horizontal")
        self.assertTrue(response.closed)

    def test_uuid_and_rendition_are_validated_before_delivery(self):
        client = AtlasClient("https://atlas.example", session=FakeSession(FakeResponse()))
        with self.assertRaises(AtlasInvalidRequestError):
            client.open_content("not-a-uuid", "horizontal")
        with self.assertRaises(AtlasInvalidRequestError):
            client.open_content(str(uuid4()), "square")

    def test_typed_http_errors(self):
        cases = ((422, AtlasInvalidRequestError), (503, AtlasUnavailableError), (500, AtlasServerError))
        for status, error in cases:
            with self.subTest(status=status), self.assertRaises(error):
                AtlasClient("https://atlas.example", session=FakeSession(FakeResponse(status, {"message": "no"}))).search({})

    def test_malformed_success_response_is_protocol_error(self):
        client = AtlasClient("https://atlas.example", session=FakeSession(FakeResponse(body={"candidates": []})))
        with self.assertRaises(AtlasProtocolError):
            client.search({})

    def test_malformed_candidate_is_protocol_error(self):
        response = {"schema_version": "asset_candidates_v1", "candidates": [{"asset_uid": str(uuid4())}], "scarcity_reason": None}
        client = AtlasClient("https://atlas.example", session=FakeSession(FakeResponse(body=response)))
        with self.assertRaises(AtlasProtocolError):
            client.search({})
