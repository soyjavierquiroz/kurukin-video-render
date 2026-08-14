import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_asset_hub import KurukinAssetHubAuthError
from app.custom.material_discovery import (
    MaterialDiscoveryError,
    discover_asset_hub_title_fallback_candidates,
    discover_material_candidates,
)
from app.custom.material_source_policy import (
    AssetHubCatalogPolicy,
    AssetHubIncludePolicy,
    CatalogExpansionRequired,
    MaterialProviderPolicy,
    MaterialSourcePolicy,
    PROVIDER_ASSET_HUB,
    PROVIDER_COVERR,
    PROVIDER_LOCAL,
    PROVIDER_PEXELS,
    PROVIDER_PIXABAY,
)
from app.models.schema import MaterialInfo
from app.services import material


def policy(*providers):
    kwargs = {}
    if PROVIDER_ASSET_HUB in providers:
        kwargs["asset_hub"] = AssetHubCatalogPolicy(include=AssetHubIncludePolicy(generic=True))
    return MaterialSourcePolicy(MaterialProviderPolicy(providers), **kwargs)


def title_policy(title):
    return MaterialSourcePolicy(
        MaterialProviderPolicy((PROVIDER_ASSET_HUB,)),
        AssetHubCatalogPolicy(include=AssetHubIncludePolicy(titles=(title,))),
    )


class FakeAssetHub:
    def __init__(self, results=None, error=None):
        self.results = results or {}
        self.error = error
        self.calls = []

    def search(self, *, query, source_policy):
        self.calls.append((query, source_policy))
        if self.error:
            raise self.error
        return self.results.get(query, [])


def stock(provider, asset_id, *, filename=None):
    item = MaterialInfo(provider=provider, url=f"https://{provider}.example/{asset_id}", duration=8)
    item.source_info = {"asset_id": asset_id, "rendition": {"width": 1080, "height": 1920}}
    if filename:
        item.filename = filename
    return item


class TestMaterialDiscovery(unittest.TestCase):
    def test_stock_wrapper_delegates_to_cache_entrypoint(self):
        if not hasattr(material, "search_videos_for_provider"):
            self.skipTest("full-suite provider-import guard supplied a minimal material module")
        cached = [stock("pexels", "cached")]
        with patch("app.services.material._search_videos_with_cache", return_value=cached) as cache:
            self.assertEqual(material.search_videos_for_provider("pexels", "term", 3, "9:16"), cached)
        self.assertEqual(cache.call_args.args[0], "pexels")
        self.assertEqual(cache.call_args.args[2:4], ("term", 3))

    def test_all_open_providers_are_searched_without_short_circuit(self):
        hub = FakeAssetHub({"es": [{"asset_uid": "hub-1", "filename": "one.mp4", "orientation": "vertical"}]})
        calls = []

        def search(provider, term, duration, aspect):
            calls.append((provider, term, duration, aspect))
            return [stock(provider, provider)]

        with patch("app.custom.material_discovery.material.search_videos_for_provider", search, create=True):
            result = discover_material_candidates(policy=policy(PROVIDER_ASSET_HUB, PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR), stock_terms=["en"], asset_hub_terms=["es"], asset_hub_provider=hub)
        self.assertEqual([call[0] for call in calls], ["pexels", "pixabay", "coverr"])
        self.assertEqual(hub.calls[0][0], "es")
        self.assertEqual(result.providers_attempted, ("asset_hub", "pexels", "pixabay", "coverr"))

    def test_terms_fallback_and_disabled_hub_is_not_instantiated(self):
        calls = []
        with patch("app.custom.material_discovery.KurukinAssetProvider", side_effect=AssertionError("must not instantiate")), patch("app.custom.material_discovery.material.search_videos_for_provider", side_effect=lambda *args: calls.append(args) or [], create=True):
            result = discover_material_candidates(policy=policy(PROVIDER_PEXELS), stock_terms=["  one ", "", "one", "two"])
        self.assertEqual(result.terms_used, {"stock": ("one", "two"), "asset_hub": ("one", "two")})
        self.assertEqual(len(calls), 2)

    def test_identity_dedupe_and_sanitization(self):
        hub = FakeAssetHub({"term": [
            {"asset_uid": "a", "filename": "same.mp4", "orientation": "vertical", "drive_file_id": "private", "nested": {"API_KEY": "secret"}},
            {"asset_uid": "b", "filename": "same.mp4", "orientation": "vertical", "remote_path": "/private"},
            {"asset_uid": "a", "filename": "duplicate.mp4", "orientation": "vertical"},
        ]})
        def search(provider, *_args):
            return [stock(provider, "x", filename="same.mp4")]
        with patch("app.custom.material_discovery.material.search_videos_for_provider", search, create=True):
            result = discover_material_candidates(policy=policy(PROVIDER_ASSET_HUB, PROVIDER_PEXELS, PROVIDER_PIXABAY), stock_terms=["term"], asset_hub_provider=hub)
        self.assertEqual([item.dedupe_key for item in result.candidates], ["kurukin_media:a", "kurukin_media:b", "pexels:x", "pixabay:x"])
        self.assertEqual(result.candidates[0].canonical_id, "a")
        self.assertNotIn("drive_file_id", str(result.candidates[0].source_info).lower())
        self.assertNotIn("secret", str(result.candidates[0].source_info).lower())

    def test_asset_hub_retries_once_with_simplified_visual_word(self):
        hub = FakeAssetHub({
            "mujer triste": [],
            "triste": [{"asset_uid": "sad-1", "filename": "sad.mp4", "orientation": "vertical"}],
        })

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            stock_terms=["mujer triste"],
            asset_hub_provider=hub,
        )

        self.assertEqual([call[0] for call in hub.calls], ["mujer triste", "triste"])
        self.assertEqual(result.candidates[0].canonical_id, "sad-1")
        self.assertEqual(
            [(item.term, item.candidate_count) for item in result.diagnostics],
            [("mujer triste", 0), ("triste", 1)],
        )

    def test_asset_hub_does_not_use_title_only_fallback_per_term(self):
        hub = FakeAssetHub({
            "pareja abrazandose": [],
            "pareja": [],
            "mi-otra-yo": [{"asset_uid": "title-1", "filename": "title.mp4", "orientation": "vertical"}],
        })

        result = discover_material_candidates(
            policy=title_policy("mi-otra-yo"),
            stock_terms=["pareja abrazandose"],
            asset_hub_provider=hub,
        )

        self.assertEqual([call[0] for call in hub.calls], ["pareja abrazandose", "pareja"])
        self.assertTrue(all(
            call[1] == {"sources": [{"scope": "title", "title": "mi-otra-yo"}]}
            for call in hub.calls
        ))
        self.assertEqual(result.candidates, ())

    def test_asset_hub_consults_all_terms_before_global_fallback(self):
        hub = FakeAssetHub({
            "pareja discutiendo": [],
            "pareja": [],
            "niño solo": [],
            "solo": [{"asset_uid": "solo-1", "filename": "solo.mp4", "orientation": "vertical"}],
        })

        result = discover_material_candidates(
            policy=title_policy("mi-otra-yo"),
            stock_terms=["pareja discutiendo", "niño solo"],
            asset_hub_provider=hub,
        )

        self.assertEqual([call[0] for call in hub.calls], ["pareja discutiendo", "pareja", "niño solo", "solo"])
        self.assertEqual([item.canonical_id for item in result.candidates], ["solo-1"])

    def test_asset_hub_title_only_global_fallback_runs_once_and_marks_candidates(self):
        hub = FakeAssetHub({
            "mi-otra-yo": [
                {"asset_uid": "title-1", "filename": "title.mp4", "orientation": "vertical"},
                {"asset_uid": "title-2", "filename": "title2.mp4", "orientation": "vertical"},
            ],
        })

        result = discover_asset_hub_title_fallback_candidates(
            policy=title_policy("mi-otra-yo"),
            asset_hub_provider=hub,
        )

        self.assertEqual([call[0] for call in hub.calls], ["mi-otra-yo"])
        self.assertEqual([item.canonical_id for item in result.candidates], ["title-1", "title-2"])
        self.assertTrue(all(
            item.source_info.get("discovery_fallback") == "title_only"
            for item in result.candidates
        ))

    def test_asset_hub_open_policy_does_not_use_title_only_fallback(self):
        hub = FakeAssetHub({"mujer preocupada": [], "preocupada": []})

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            stock_terms=["mujer preocupada"],
            asset_hub_provider=hub,
        )

        self.assertEqual([call[0] for call in hub.calls], ["mujer preocupada", "preocupada"])
        self.assertEqual(result.candidates, ())

    def test_empty_is_success_and_partial_failure_continues(self):
        def search(provider, *_args):
            if provider == "pexels":
                raise RuntimeError("503 upstream key=secret")
            return []
        with patch("app.custom.material_discovery.material.search_videos_for_provider", search, create=True):
            result = discover_material_candidates(policy=policy(PROVIDER_PEXELS, PROVIDER_PIXABAY), stock_terms=["term"])
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.providers_succeeded, ("pixabay",))
        self.assertNotIn("secret", str(result.diagnostics).lower())

    def test_only_provider_failure_and_hub_auth_are_fatal(self):
        with patch("app.custom.material_discovery.material.search_videos_for_provider", side_effect=RuntimeError("503"), create=True):
            with self.assertRaises(MaterialDiscoveryError):
                discover_material_candidates(policy=policy(PROVIDER_PEXELS), stock_terms=["term"])
        with self.assertRaises(KurukinAssetHubAuthError):
            discover_material_candidates(policy=policy(PROVIDER_ASSET_HUB), stock_terms=["term"], asset_hub_provider=FakeAssetHub(error=KurukinAssetHubAuthError("401")))

    def test_catalog_expansion_does_no_http_and_local_does_no_crawl(self):
        broad = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_ASSET_HUB,)), AssetHubCatalogPolicy(include=AssetHubIncludePolicy(all_titles=True)))
        with patch("app.custom.material_discovery.KurukinAssetProvider", side_effect=AssertionError("no HTTP")):
            with self.assertRaises(CatalogExpansionRequired):
                discover_material_candidates(policy=broad, stock_terms=["term"])
        result = discover_material_candidates(policy=policy(PROVIDER_LOCAL), stock_terms=["term"])
        self.assertEqual(result.diagnostics[0].status, "pending_adapter")


if __name__ == "__main__":
    unittest.main()
