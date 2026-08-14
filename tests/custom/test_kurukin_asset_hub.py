import os
import sys
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_asset_hub import (  # noqa: E402
    KurukinAssetHubAuthError,
    KurukinAssetHubUnavailableError,
    KurukinAssetHubValidationError,
    KurukinAssetProvider,
    build_explicit_bundle_payload,
    dedupe_key,
    normalize_asset_identity,
    normalize_source_policy,
    resolve_ready_asset_paths,
    validate_materialized_path,
)
from app.custom.asset_hub_manifest import (  # noqa: E402
    get_asset_hub_job_assets_dir,
    is_asset_hub_asset_ready,
)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_provider(responses, *, sleeper=None):
    return KurukinAssetProvider(
        base_url="https://asset-hub.test",
        api_key="super-secret-key",
        session=FakeSession(responses),
        sleeper=sleeper or (lambda _delay: None),
        backoff_seconds=(0, 0),
    )


class KurukinAssetHubTests(unittest.TestCase):
    def test_search_generic(self):
        provider = make_provider([FakeResponse(200, {"count": 1, "assets": [{"asset_uid": "drive-1"}]})])

        assets = provider.search(
            query="woman",
            limit=20,
            source_policy={"sources": [{"scope": "generic"}]},
        )

        self.assertEqual(assets[0]["asset_uid"], "drive-1")
        body = provider.session.calls[0]["json"]
        self.assertEqual(body["source_policy"], {"sources": [{"scope": "generic"}]})

    def test_search_without_source_policy_defaults_to_generic_only(self):
        provider = make_provider([FakeResponse(200, {"assets": []})])

        provider.search(query="woman", limit=20)

        self.assertEqual(
            provider.session.calls[0]["json"]["source_policy"],
            {"sources": [{"scope": "generic"}]},
        )
        self.assertEqual(normalize_source_policy(None), {"sources": [{"scope": "generic"}]})

    def test_search_title_mi_otra_yo(self):
        provider = make_provider([FakeResponse(200, {"assets": [{"asset_uid": "drive-2"}]})])

        provider.search(
            query="otra yo",
            limit=20,
            source_policy={"sources": [{"scope": "title", "title": "mi-otra-yo"}]},
        )

        self.assertEqual(
            provider.session.calls[0]["json"]["source_policy"],
            {"sources": [{"scope": "title", "title": "mi-otra-yo"}]},
        )

    def test_search_title_plus_generic_preserves_or_sources(self):
        provider = make_provider([FakeResponse(200, {"assets": []})])

        provider.search(
            query="light",
            limit=20,
            source_policy={
                "sources": [
                    {"scope": "title", "title": "mi-otra-yo"},
                    {"scope": "generic"},
                ]
            },
        )

        self.assertEqual(
            provider.session.calls[0]["json"]["source_policy"]["sources"],
            [{"scope": "title", "title": "mi-otra-yo"}, {"scope": "generic"}],
        )

    def test_search_brand_grandiosa_mujer(self):
        provider = make_provider([FakeResponse(200, {"assets": []})])

        provider.search(
            query="brand",
            limit=20,
            source_policy={"sources": [{"scope": "brand", "brand": "grandiosa-mujer"}]},
        )

        self.assertEqual(
            provider.session.calls[0]["json"]["source_policy"],
            {"sources": [{"scope": "brand", "brand": "grandiosa-mujer"}]},
        )

    def test_search_accepts_legacy_string_asset_id_as_asset_uid(self):
        provider = make_provider([FakeResponse(200, {"assets": [{"asset_id": "drive-52cee0a6"}]})])

        self.assertEqual(
            provider.search(query="legacy", source_policy={"sources": [{"scope": "generic"}]})[0]["asset_uid"],
            "drive-52cee0a6",
        )

    def test_search_rejects_integer_asset_id_without_asset_uid(self):
        provider = make_provider([FakeResponse(200, {"assets": [{"asset_id": 123}]})])

        with self.assertRaises(KurukinAssetHubValidationError):
            provider.search(query="legacy", source_policy={"sources": [{"scope": "generic"}]})

    def test_normalize_rejects_asset_id_fallback_outside_search(self):
        with self.assertRaises(KurukinAssetHubValidationError):
            normalize_asset_identity({"asset_id": "drive-52cee0a6"})

    def test_normalize_rejects_integer_asset_uid(self):
        with self.assertRaises(KurukinAssetHubValidationError):
            normalize_asset_identity({"asset_uid": 123})

    def test_dedupe_key(self):
        self.assertEqual(dedupe_key("drive-52cee0a6"), "kurukin_media:drive-52cee0a6")

    def test_create_explicit_bundle_with_asset_uid_per_scene(self):
        provider = make_provider([FakeResponse(200, {"bundle_uid": "bundle-1"})])

        result = provider.create_bundle(
            job_id="mpt-001",
            scenes=[
                {
                    "scene_id": "scene-001",
                    "scene_index": 1,
                    "script_scene": "hello",
                    "selected_asset_uids": ["drive-a", "drive-b"],
                }
            ],
        )

        self.assertEqual(result["bundle_uid"], "bundle-1")
        payload = provider.session.calls[0]["json"]
        self.assertNotIn("brand_slug", payload)
        self.assertEqual(
            payload["scenes"][0]["selected_asset_uids"],
            ["drive-a", "drive-b"],
        )

    def test_create_bundle_503_does_not_retry(self):
        provider = make_provider([FakeResponse(503, {}), FakeResponse(200, {"bundle_uid": "bundle-1"})])

        with self.assertRaises(KurukinAssetHubUnavailableError):
            provider.create_bundle(
                job_id="mpt-001",
                scenes=[{"selected_asset_uids": ["drive-a"]}],
            )

        self.assertEqual(len(provider.session.calls), 1)

    def test_create_bundle_timeout_does_not_retry(self):
        provider = make_provider(
            [requests.Timeout("timeout"), FakeResponse(200, {"bundle_uid": "bundle-1"})]
        )

        with self.assertRaises(KurukinAssetHubUnavailableError):
            provider.create_bundle(
                job_id="mpt-001",
                scenes=[{"selected_asset_uids": ["drive-a"]}],
            )

        self.assertEqual(len(provider.session.calls), 1)

    def test_materialize_bundle_uses_longer_timeout(self):
        provider = make_provider([FakeResponse(200, {"status": "ready"})])

        provider.materialize_bundle("bundle-1")

        self.assertEqual(provider.session.calls[0]["timeout"], 90)

    def test_materialize_timeout_can_be_overridden_by_env(self):
        provider = KurukinAssetProvider(
            base_url="https://asset-hub.test",
            api_key="super-secret-key",
            session=FakeSession([FakeResponse(200, {"status": "ready"})]),
            sleeper=lambda _delay: None,
            backoff_seconds=(0, 0),
            env={
                "ASSET_HUB_MATERIALIZE_TIMEOUT_SECONDS": "180",
            },
        )

        provider.materialize_bundle("bundle-1")

        self.assertEqual(provider.session.calls[0]["timeout"], 180)

    def test_explicit_only_bundle_omits_brand_slug(self):
        payload = build_explicit_bundle_payload(
            job_id="mpt-001",
            scenes=[{"selected_asset_uids": ["drive-a"]}],
        )

        self.assertNotIn("brand_slug", payload)

    def test_count_matches_selected_asset_uids_when_present(self):
        payload = build_explicit_bundle_payload(
            job_id="mpt-001",
            scenes=[{"count": 99, "selected_asset_uids": ["drive-a", "drive-b"]}],
        )

        self.assertEqual(payload["scenes"][0]["count"], 2)

    def test_parse_renderer_manifest_ready_assets(self):
        manifest = {
            "scenes": [
                {
                    "assets": [
                        {
                            "asset_uid": "drive-a",
                            "status": "ready",
                            "local_path": "/data/job-assets/bundle/clip.mp4",
                            "relative_path": "bundle/clip.mp4",
                            "size_bytes": 123,
                            "sha256": "abc",
                            "drive_file_id": "ignored",
                        }
                    ]
                }
            ]
        }

        assets = resolve_ready_asset_paths(manifest)

        self.assertEqual(
            assets,
            [
                {
                    "asset_uid": "drive-a",
                    "local_path": "/data/job-assets/bundle/clip.mp4",
                    "relative_path": "bundle/clip.mp4",
                    "size_bytes": 123,
                    "sha256": "abc",
                }
            ],
        )

    def test_resolve_local_path_only_for_ready(self):
        manifest = {
            "scenes": [
                {
                    "assets": [
                        {"asset_uid": "ready", "materialization_status": "ready", "local_path": "/data/job-assets/a.mp4"},
                        {"asset_uid": "pending", "materialization_status": "pending", "local_path": "/data/job-assets/b.mp4"},
                    ]
                }
            ]
        }

        self.assertEqual(
            [asset["asset_uid"] for asset in resolve_ready_asset_paths(manifest)],
            ["ready"],
        )

    def test_ready_status_materialization_status_takes_precedence(self):
        self.assertTrue(
            is_asset_hub_asset_ready({"materialization_status": "ready", "status": "ready"})
        )
        self.assertTrue(is_asset_hub_asset_ready({"materialization_status": "ready"}))
        self.assertTrue(is_asset_hub_asset_ready({"status": "ready"}))
        self.assertFalse(
            is_asset_hub_asset_ready({"materialization_status": "pending", "status": "ready"})
        )
        self.assertFalse(
            is_asset_hub_asset_ready({"materialization_status": "failed", "status": "ready"})
        )
        self.assertFalse(is_asset_hub_asset_ready({"status": "available"}))
        self.assertFalse(is_asset_hub_asset_ready({}))

    def test_ready_status_rejects_non_contractual_aliases(self):
        self.assertFalse(is_asset_hub_asset_ready({"status": "materialized"}))
        self.assertFalse(is_asset_hub_asset_ready({"status": "available"}))
        self.assertFalse(is_asset_hub_asset_ready({"materialization_status": "materialized"}))
        self.assertFalse(is_asset_hub_asset_ready({"materialization_status": "available"}))

    def test_asset_with_local_path_but_without_status_is_not_consumed(self):
        manifest = {
            "scenes": [
                {
                    "assets": [
                        {"asset_uid": "not-ready", "local_path": "/data/job-assets/a.mp4"},
                    ]
                }
            ]
        }

        self.assertEqual(resolve_ready_asset_paths(manifest), [])

    def test_rejects_local_path_outside_materialized_root(self):
        with self.assertRaises(KurukinAssetHubValidationError):
            validate_materialized_path("/tmp/outside.mp4")

    def test_401_without_retry(self):
        provider = make_provider([FakeResponse(401, {})])

        with self.assertRaises(KurukinAssetHubAuthError):
            provider.search(query="x", source_policy={"sources": [{"scope": "generic"}]})

        self.assertEqual(len(provider.session.calls), 1)

    def test_403_without_retry(self):
        provider = make_provider([FakeResponse(403, {})])

        with self.assertRaises(KurukinAssetHubAuthError):
            provider.search(query="x", source_policy={"sources": [{"scope": "generic"}]})

        self.assertEqual(len(provider.session.calls), 1)

    def test_422_without_retry(self):
        provider = make_provider([FakeResponse(422, {})])

        with self.assertRaises(KurukinAssetHubValidationError):
            provider.search(query="x", source_policy={"sources": [{"scope": "generic"}]})

        self.assertEqual(len(provider.session.calls), 1)

    def test_404_without_retry(self):
        provider = make_provider([FakeResponse(404, {})])

        with self.assertRaises(KurukinAssetHubValidationError):
            provider.get_renderer_manifest("missing")

        self.assertEqual(len(provider.session.calls), 1)

    def test_503_retries_limited(self):
        sleeps = []
        provider = make_provider(
            [FakeResponse(503, {}), FakeResponse(503, {}), FakeResponse(503, {})],
            sleeper=sleeps.append,
        )

        with self.assertRaises(KurukinAssetHubUnavailableError):
            provider.get_renderer_manifest("bundle")

        self.assertEqual(len(provider.session.calls), 3)
        self.assertEqual(sleeps, [])

    def test_500_502_504_retries_limited(self):
        for status in (500, 502, 504):
            provider = make_provider([FakeResponse(status, {}), FakeResponse(200, {"ok": True})])
            self.assertEqual(provider.get_renderer_manifest("bundle"), {"ok": True})
            self.assertEqual(len(provider.session.calls), 2)

    def test_timeout_and_connection_errors_retry_limited(self):
        provider = make_provider(
            [
                requests.Timeout("timeout"),
                requests.ConnectionError("connection"),
                FakeResponse(200, {"ok": True}),
            ]
        )

        self.assertEqual(provider.get_renderer_manifest("bundle"), {"ok": True})
        self.assertEqual(len(provider.session.calls), 3)

    def test_count_zero_returns_empty_list(self):
        provider = make_provider([FakeResponse(200, {"count": 0, "assets": []})])

        self.assertEqual(
            provider.search(query="none", source_policy={"sources": [{"scope": "generic"}]}),
            [],
        )

    def test_api_key_not_in_exception(self):
        provider = make_provider([FakeResponse(500, {}), FakeResponse(500, {}), FakeResponse(500, {})])

        with self.assertRaises(KurukinAssetHubUnavailableError) as caught:
            provider.get_renderer_manifest("bundle")

        self.assertNotIn("super-secret-key", str(caught.exception))

    def test_consumption_does_not_require_drive_file_id(self):
        manifest = {"scenes": [{"assets": [{"asset_uid": "a", "status": "ready", "local_path": "/data/job-assets/a.mp4"}]}]}

        self.assertEqual(resolve_ready_asset_paths(manifest)[0]["asset_uid"], "a")

    def test_consumption_does_not_require_remote_path(self):
        manifest = {"scenes": [{"assets": [{"asset_uid": "a", "status": "ready", "local_path": "/data/job-assets/a.mp4"}]}]}

        self.assertEqual(resolve_ready_asset_paths(manifest)[0]["local_path"], "/data/job-assets/a.mp4")

    def test_consumption_does_not_require_rclone_remote(self):
        manifest = {"scenes": [{"assets": [{"asset_uid": "a", "status": "ready", "local_path": "/data/job-assets/a.mp4"}]}]}

        self.assertEqual(len(resolve_ready_asset_paths(manifest)), 1)

    def test_selected_asset_uids_preserve_order_rank(self):
        payload = build_explicit_bundle_payload(
            job_id="mpt-001",
            scenes=[{"selected_asset_uids": ["third", "first", "second"]}],
        )

        self.assertEqual(
            payload["scenes"][0]["selected_asset_uids"],
            ["third", "first", "second"],
        )

    def test_selected_asset_uids_reject_integer(self):
        with self.assertRaises(KurukinAssetHubValidationError):
            build_explicit_bundle_payload(
                job_id="mpt-001",
                scenes=[{"selected_asset_uids": [123]}],
            )

    def test_parser_tolerates_extra_manifest_fields(self):
        manifest = {
            "extra": "ignored",
            "scenes": [
                {
                    "assets": [
                        {
                            "asset_uid": "a",
                            "asset_id": 123,
                            "status": "ready",
                            "local_path": "/data/job-assets/a.mp4",
                            "remote_path": "ignored",
                            "rclone_remote": "ignored",
                            "unexpected": {"nested": True},
                        }
                    ]
                }
            ],
        }

        self.assertEqual(resolve_ready_asset_paths(manifest)[0]["asset_uid"], "a")

    def test_manifest_without_asset_uid_is_rejected(self):
        manifest = {
            "scenes": [
                {
                    "assets": [
                        {
                            "asset_id": "123",
                            "status": "ready",
                            "local_path": "/data/job-assets/a.mp4",
                        }
                    ]
                }
            ],
        }

        with self.assertRaises(KurukinAssetHubValidationError):
            resolve_ready_asset_paths(manifest)

    def test_manifest_with_asset_uid_ignores_integer_asset_id(self):
        manifest = {
            "scenes": [
                {
                    "assets": [
                        {
                            "asset_uid": "a",
                            "asset_id": 123,
                            "status": "ready",
                            "local_path": "/data/job-assets/a.mp4",
                        }
                    ]
                }
            ],
        }

        self.assertEqual(resolve_ready_asset_paths(manifest)[0]["asset_uid"], "a")

    def test_materialized_root_env_is_used_for_path_validation(self):
        original = os.environ.get("ASSET_HUB_MATERIALIZED_ROOT")
        try:
            os.environ["ASSET_HUB_MATERIALIZED_ROOT"] = "/tmp/asset-root"
            self.assertEqual(
                validate_materialized_path("/tmp/asset-root/bundle/a.mp4"),
                "/tmp/asset-root/bundle/a.mp4",
            )
        finally:
            if original is None:
                os.environ.pop("ASSET_HUB_MATERIALIZED_ROOT", None)
            else:
                os.environ["ASSET_HUB_MATERIALIZED_ROOT"] = original

    def test_empty_materialized_root_falls_back_to_default_job_assets_dir(self):
        original_root = os.environ.get("ASSET_HUB_MATERIALIZED_ROOT")
        original_legacy = os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        try:
            os.environ["ASSET_HUB_MATERIALIZED_ROOT"] = ""
            os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)

            self.assertEqual(get_asset_hub_job_assets_dir(), Path("/data/job-assets"))
        finally:
            if original_root is None:
                os.environ.pop("ASSET_HUB_MATERIALIZED_ROOT", None)
            else:
                os.environ["ASSET_HUB_MATERIALIZED_ROOT"] = original_root
            if original_legacy is None:
                os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)
            else:
                os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = original_legacy

    def test_empty_materialized_root_falls_back_to_legacy_job_assets_dir(self):
        original_root = os.environ.get("ASSET_HUB_MATERIALIZED_ROOT")
        original_legacy = os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        try:
            os.environ["ASSET_HUB_MATERIALIZED_ROOT"] = ""
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = "/tmp/legacy-job-assets"

            self.assertEqual(get_asset_hub_job_assets_dir(), Path("/tmp/legacy-job-assets"))
        finally:
            if original_root is None:
                os.environ.pop("ASSET_HUB_MATERIALIZED_ROOT", None)
            else:
                os.environ["ASSET_HUB_MATERIALIZED_ROOT"] = original_root
            if original_legacy is None:
                os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)
            else:
                os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = original_legacy


if __name__ == "__main__":
    unittest.main()
