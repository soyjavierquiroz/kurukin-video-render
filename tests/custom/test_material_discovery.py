import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_asset_hub import (
    KurukinAssetHubAuthError,
    KurukinAssetHubUnavailableError,
    KurukinAssetHubValidationError,
)
from app.custom.asset_search_v2 import build_visual_queries_v2
from app.custom.material_discovery import (
    TITLE_PREFERRED_MIN_CANDIDATES,
    MaterialDiscoveryError,
    discover_asset_hub_title_fallback_candidates,
    discover_asset_hub_review_reserve_candidates,
    discover_material_candidates,
    provider_diagnostics_for_review,
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
from app.custom.material_provider_availability import native_stock_provider_configured
from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
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

    def search(self, *, query, source_policy, limit=20):
        self.calls.append((query, source_policy, limit))
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
    def setUp(self):
        # Discovery wiring tests model configured MPT providers; the explicit
        # missing-config test below overrides this guard.
        self._configured = patch(
            "app.custom.material_discovery.native_stock_provider_configured",
            return_value=True,
        )
        self._configured.start()
        self.addCleanup(self._configured.stop)

    def assertDiverseQueries(self, queries):
        token_sets = [frozenset(query.lower().split()) for query in queries]
        self.assertEqual(len(token_sets), len(set(token_sets)))
        for left_index, left in enumerate(token_sets):
            for right in token_sets[left_index + 1:]:
                self.assertNotEqual(left, right)

    def _run_generales_native_stock_discovery(self, video_source):
        """Run Kurukin's real stock discovery path with no cache or network."""
        configured_keys = {
            "pexels_api_keys": ["test-pexels-key"],
            "pixabay_api_keys": ["test-pixabay-key"],
            "coverr_api_keys": ["test-coverr-key"],
            "video_source": video_source,
        }
        with patch.dict(config.app, configured_keys), \
             patch(
                 "app.custom.material_discovery.native_stock_provider_configured",
                 wraps=native_stock_provider_configured,
             ), \
             patch.object(material.material_cache, "load_material_search_cache", return_value=None), \
             patch.object(material.material_cache, "get_material_search_cache_lock", return_value=nullcontext()), \
             patch.object(material.material_cache, "save_material_search_cache"), \
             patch.object(material, "search_videos_pexels", return_value=[stock("pexels", "native-pexels")]) as pexels, \
             patch.object(material, "search_videos_pixabay", return_value=[stock("pixabay", "native-pixabay")]) as pixabay, \
             patch.object(material, "search_videos_coverr", return_value=[stock("coverr", "native-coverr")]) as coverr:
            result = discover_material_candidates(
                policy=policy(PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR),
                stock_terms=["woman worried at home"],
            )
        return result, {"pexels": pexels, "pixabay": pixabay, "coverr": coverr}

    def test_generales_explicitly_dispatches_every_native_mpt_stock_provider(self):
        result, searches = self._run_generales_native_stock_discovery("pexels")

        for provider, search in searches.items():
            with self.subTest(provider=provider):
                search.assert_called_once_with(
                    search_term="woman worried at home",
                    minimum_duration=0,
                    video_aspect=VideoAspect.portrait,
                )
        self.assertEqual(
            {candidate.provider for candidate in result.candidates},
            {PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR},
        )

    def test_generales_dispatch_is_independent_of_vanilla_video_source(self):
        called_sets = {}
        for video_source in ("pexels", "pixabay"):
            with self.subTest(video_source=video_source):
                _result, searches = self._run_generales_native_stock_discovery(video_source)
                called_sets[video_source] = {
                    provider for provider, search in searches.items() if search.called
                }
                self.assertEqual(called_sets[video_source], {
                    PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR,
                })

        self.assertEqual(called_sets["pexels"], called_sets["pixabay"])

    def test_native_stock_entrypoints_request_provider_specific_key_lists(self):
        native_searches = (
            ("pexels_api_keys", material.search_videos_pexels),
            ("pixabay_api_keys", material.search_videos_pixabay),
            ("coverr_api_keys", material.search_videos_coverr),
        )
        with patch.object(material, "get_api_key", return_value="test-key") as get_api_key, \
             patch.object(material.requests, "get", side_effect=AssertionError("network disabled")):
            for _key_name, search in native_searches:
                search("woman worried at home", 0, VideoAspect.portrait)

        self.assertEqual(
            [call.args[0] for call in get_api_key.call_args_list],
            [key_name for key_name, _search in native_searches],
        )

    def test_visual_query_v2_compacts_long_conceptual_scene(self):
        queries = build_visual_queries_v2(
            "Aprendiste a ser fuerte porque sentías que nadie iba a rescatarte"
        )

        self.assertGreaterEqual(len(queries), 2)
        self.assertLessEqual(len(queries), 3)
        self.assertTrue(all(3 <= len(query.split()) <= 7 for query in queries))
        self.assertTrue(any("soledad" in query or "aislamiento" in query for query in queries))
        self.assertDiverseQueries(queries)

    def test_visual_query_v2_does_not_invent_concrete_details(self):
        queries = build_visual_queries_v2("Ella respiró profundo y decidió seguir adelante")
        serialized = " ".join(queries).lower()

        for invented in ("bosque", "vela", "noche", "azul", "anciana"):
            self.assertNotIn(invented, serialized)

    def test_visual_query_v2_maps_editorial_concepts_without_noise(self):
        examples = (
            (
                "También necesitó que alguien la escuchara, la protegiera y le dijera que todo iba a estar bien.",
                ("apoyo emocional", "consuelo", "protección emocional"),
            ),
            (
                "La que sostiene a los demás aunque también esté asustada.",
                ("cuidadora", "responsabilidad emocional", "preocupada"),
            ),
            (
                "Elegís personas que necesitan ser rescatadas.",
                ("dependencia emocional", "apoyo emocional", "relación desequilibrada"),
            ),
        )
        noisy = {"también", "demas", "demás", "elegís", "necesitó", "necesitan"}
        for scene, expected_terms in examples:
            with self.subTest(scene=scene):
                queries = build_visual_queries_v2(scene, [scene])
                serialized = " ".join(queries).lower()
                self.assertLessEqual(len(queries), 3)
                self.assertTrue(all(3 <= len(query.split()) <= 7 for query in queries))
                self.assertDiverseQueries(queries)
                self.assertTrue(any(term in serialized for term in expected_terms))
                self.assertFalse(noisy & set(serialized.split()))
                self.assertNotIn("persona demás también", serialized)

    def test_visual_query_v2_keeps_scene_queries_with_incompatible_hint(self):
        queries = build_visual_queries_v2(
            "Por eso descansar te da culpa. Recibir te incomoda.",
            ["niña sola"],
        )
        serialized = " ".join(queries).lower()

        self.assertGreaterEqual(len(queries), 2)
        self.assertIn("culpa", serialized)
        self.assertTrue("apoyo emocional" in serialized or "agotamiento" in serialized)
        self.assertNotEqual(queries, ("niña sola",))

    def test_visual_query_v2_compatible_hint_can_complement_scene(self):
        queries = build_visual_queries_v2(
            "Recibir ayuda te incomoda.",
            ["apoyo emocional"],
        )
        serialized = " ".join(queries).lower()

        self.assertIn("incomoda", serialized)
        self.assertIn("apoyo emocional", serialized)
        self.assertDiverseQueries(queries)

    def test_visual_query_v2_hint_never_collapses_conceptual_scene(self):
        queries = build_visual_queries_v2(
            "La fortaleza que un día te protegió no tiene que convertirse en la prisión donde vivas.",
            ["mujer trabajando"],
        )
        serialized = " ".join(queries).lower()

        self.assertGreaterEqual(len(queries), 2)
        self.assertNotEqual(queries, ("mujer trabajando",))
        self.assertTrue("fortaleza" in serialized or "carga emocional" in serialized)
        self.assertNotIn("trabajando", serialized)

    def test_visual_query_v2_empty_scene_ignores_existing_hint(self):
        self.assertEqual(build_visual_queries_v2("   ", ["niña sola"]), ())

    def test_visual_query_v2_feminine_editorial_profile_prioritizes_feminine_subject(self):
        queries = build_visual_queries_v2(
            "Y tú también tienes derecho a construir una vida propia.",
            editorial_profile={"subject_gender": "feminine"},
        )

        serialized = " ".join(queries).lower()
        self.assertTrue(queries)
        self.assertTrue(any(term in serialized for term in ("mujer", "niña", "madre", "hermana")))
        self.assertDiverseQueries(queries)

    def test_stock_wrapper_delegates_to_cache_entrypoint(self):
        if not hasattr(material, "search_videos_for_provider"):
            self.skipTest("full-suite provider-import guard supplied a minimal material module")
        cached = [stock("pexels", "cached")]
        with patch("app.services.material._search_videos_with_cache", return_value=cached) as cache:
            self.assertEqual(material.search_videos_for_provider("pexels", "term", 3, "9:16"), cached)
        self.assertEqual(cache.call_args.args[0], "pexels")
        self.assertEqual(cache.call_args.args[2:4], ("term", 3))

    def test_all_open_providers_are_searched_without_short_circuit(self):
        hub = FakeAssetHub({"es": [{"asset_uid": "hub-1", "filename": "one.mp4", "orientation": "vertical-9x16"}]})
        calls = []

        def search(provider, term, duration, aspect):
            calls.append((provider, term, duration, aspect))
            return [stock(provider, provider)]

        with patch("app.custom.material_discovery.material.search_videos_for_provider", search, create=True):
            result = discover_material_candidates(policy=policy(PROVIDER_ASSET_HUB, PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR), stock_terms=["en"], asset_hub_terms=["es"], asset_hub_provider=hub)
        self.assertEqual([call[0] for call in calls], ["pexels", "pixabay", "coverr"])
        self.assertEqual(hub.calls[0][0], "es")
        self.assertEqual(result.providers_attempted, ("asset_hub", "pexels", "pixabay", "coverr"))

    def test_each_stock_policy_provider_calls_its_native_mpt_entrypoint(self):
        for provider in (PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR):
            with self.subTest(provider=provider), patch(
                "app.custom.material_discovery.material.search_videos_for_provider",
                return_value=[stock(provider, "native-1")],
            ) as search:
                result = discover_material_candidates(
                    policy=policy(provider), stock_terms=["woman worried at home"],
                )

            search.assert_called_once_with(provider, "woman worried at home", 0, "9:16")
            self.assertEqual(result.candidates[0].provider, provider)

    def test_terms_fallback_and_disabled_hub_is_not_instantiated(self):
        calls = []
        with patch("app.custom.material_discovery.KurukinAssetProvider", side_effect=AssertionError("must not instantiate")), patch("app.custom.material_discovery.material.search_videos_for_provider", side_effect=lambda *args: calls.append(args) or [], create=True):
            result = discover_material_candidates(policy=policy(PROVIDER_PEXELS), stock_terms=["  one ", "", "one", "two"])
        self.assertEqual(result.terms_used, {"stock": ("one", "two"), "asset_hub": ("one", "two")})
        self.assertEqual(len(calls), 2)

    def test_identity_dedupe_and_sanitization(self):
        hub = FakeAssetHub({"term": [
            {"asset_uid": "a", "filename": "same.mp4", "orientation": "vertical-9x16", "drive_file_id": "private", "nested": {"API_KEY": "secret"}},
            {"asset_uid": "b", "filename": "same.mp4", "orientation": "vertical-9x16", "remote_path": "/private"},
            {"asset_uid": "a", "filename": "duplicate.mp4", "orientation": "vertical-9x16"},
        ]})
        def search(provider, *_args):
            return [stock(provider, "x", filename="same.mp4")]
        with patch("app.custom.material_discovery.material.search_videos_for_provider", search, create=True):
            result = discover_material_candidates(policy=policy(PROVIDER_ASSET_HUB, PROVIDER_PEXELS, PROVIDER_PIXABAY), stock_terms=["term"], asset_hub_provider=hub)
        self.assertEqual([item.dedupe_key for item in result.candidates], ["kurukin_media:a", "kurukin_media:b", "pexels:x", "pixabay:x"])
        self.assertEqual(result.candidates[0].canonical_id, "a")
        self.assertNotIn("drive_file_id", str(result.candidates[0].source_info).lower())
        self.assertNotIn("secret", str(result.candidates[0].source_info).lower())

    def test_asset_hub_search_preview_metadata_is_preserved(self):
        hub = FakeAssetHub({"term": [
            {"asset_uid": "a", "orientation": "vertical-9x16", "preview_url": "https://asset-hub.example/a.jpg"},
        ]})

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            stock_terms=["term"],
            asset_hub_provider=hub,
        )

        self.assertEqual(result.candidates[0].source_info["preview_url"], "https://asset-hub.example/a.jpg")

    def test_asset_hub_search_preserves_extended_editorial_metadata(self):
        hub = FakeAssetHub({"term": [{
            "asset_uid": "editorial-1",
            "filename": "woman.mp4",
            "duration": 7.1,
            "width": 1080,
            "height": 1350,
            "orientation": "vertical-4x5",
            "primary_theme": "vulnerabilidad",
            "primary_topic": "aceptar ayuda",
            "visual_description": "Una mujer sentada mirando hacia un lado.",
            "action_description": "Permanece quieta con expresión preocupada.",
            "contains_people": True,
            "people_count": 1,
            "visual_presentation": "feminine",
            "visual_presentation_confidence": 0.98,
            "person_visibility": "clear",
        }]})

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB), stock_terms=["term"], asset_hub_provider=hub,
        )

        item = result.candidates[0]
        self.assertEqual((item.duration, item.width, item.height, item.orientation), (7.1, 1080, 1350, "vertical-4x5"))
        self.assertEqual(item.source_info["primary_theme"], "vulnerabilidad")
        self.assertEqual(item.source_info["primary_topic"], "aceptar ayuda")
        self.assertEqual(item.source_info["visual_description"], "Una mujer sentada mirando hacia un lado.")
        self.assertEqual(item.source_info["action_description"], "Permanece quieta con expresión preocupada.")
        self.assertEqual(
            {key: item.source_info[key] for key in ("contains_people", "people_count", "visual_presentation", "visual_presentation_confidence", "person_visibility")},
            {"contains_people": True, "people_count": 1, "visual_presentation": "feminine", "visual_presentation_confidence": 0.98, "person_visibility": "clear"},
        )

    def test_asset_hub_search_preserves_null_optional_descriptions(self):
        hub = FakeAssetHub({"term": [{
            "asset_uid": "editorial-null",
            "orientation": "vertical-9x16",
            "visual_description": None,
            "action_description": None,
        }]})

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB), stock_terms=["term"], asset_hub_provider=hub,
        )

        self.assertIsNone(result.candidates[0].source_info["visual_description"])
        self.assertIsNone(result.candidates[0].source_info["action_description"])

    def test_asset_hub_landscape_result_is_still_filtered_for_vertical_jobs(self):
        hub = FakeAssetHub({"term": [
            {"asset_uid": "vertical", "width": 1080, "height": 1920, "orientation": "vertical-9x16"},
            {"asset_uid": "landscape", "width": 1920, "height": 1080, "orientation": "landscape-16x9"},
        ]})

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB), stock_terms=["term"], asset_hub_provider=hub,
            video_aspect="9:16",
        )

        self.assertEqual([item.canonical_id for item in result.candidates], ["vertical"])

    def test_asset_hub_retries_once_with_simplified_visual_word(self):
        hub = FakeAssetHub({
            "mujer triste": [],
            "mujer triste tristeza": [],
            "triste": [{"asset_uid": "sad-1", "filename": "sad.mp4", "orientation": "vertical-9x16"}],
        })

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            stock_terms=["mujer triste"],
            asset_hub_provider=hub,
        )

        self.assertEqual([call[0] for call in hub.calls], ["mujer triste", "mujer triste tristeza", "triste"])
        self.assertEqual(result.candidates[0].canonical_id, "sad-1")
        self.assertEqual(result.diagnostics[-1].candidate_count, 1)

    def test_asset_hub_does_not_use_title_only_fallback_per_term(self):
        hub = FakeAssetHub({
            "pareja abrazandose": [],
            "pareja": [],
            "mi-otra-yo": [{"asset_uid": "title-1", "filename": "title.mp4", "orientation": "vertical-9x16"}],
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
            "pareja preocupación relación": [],
            "discusión familiar desacuerdo": [],
            "pareja": [],
            "niño solo": [],
            "niño solo soledad": [],
            "soledad vulnerabilidad emocional": [],
            "solo": [{"asset_uid": "solo-1", "filename": "solo.mp4", "orientation": "vertical-9x16"}],
        })

        result = discover_material_candidates(
            policy=title_policy("mi-otra-yo"),
            stock_terms=["pareja discutiendo", "niño solo"],
            asset_hub_provider=hub,
        )

        self.assertEqual([call[0] for call in hub.calls], ["pareja discutiendo", "pareja preocupación relación", "discusión familiar desacuerdo", "pareja", "niño solo", "niño solo soledad", "soledad vulnerabilidad emocional", "solo"])
        self.assertEqual([item.canonical_id for item in result.candidates], ["solo-1"])

    def test_asset_hub_title_only_global_fallback_runs_once_and_marks_candidates(self):
        hub = FakeAssetHub({
            "mi-otra-yo": [
                {"asset_uid": "title-1", "filename": "title.mp4", "orientation": "vertical-9x16"},
                {"asset_uid": "title-2", "filename": "title2.mp4", "orientation": "vertical-9x16"},
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

    def test_asset_hub_multi_query_merges_duplicate_asset_uids(self):
        hub = FakeAssetHub({
            "mujer triste": [{"asset_uid": "a", "orientation": "vertical-9x16"}],
            "mujer triste tristeza": [
                {"asset_uid": "a", "orientation": "vertical-9x16"},
                {"asset_uid": "b", "orientation": "vertical-9x16"},
            ],
            "triste": [{"asset_uid": "c", "orientation": "vertical-9x16"}],
        })

        result = discover_material_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            stock_terms=["mujer triste"],
            asset_hub_provider=hub,
        )

        self.assertEqual([item.canonical_id for item in result.candidates], ["a", "b", "c"])

    def test_asset_hub_search_terms_store_real_multi_query_provenance(self):
        hub = FakeAssetHub({
            "Por eso descansar te da culpa": [],
            "persona culpa agotamiento": [{"asset_uid": "a", "orientation": "vertical-9x16"}],
            "persona descanso": [{"asset_uid": "b", "orientation": "vertical-9x16"}],
        })

        result = discover_asset_hub_review_reserve_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            terms=["Por eso descansar te da culpa"],
            asset_hub_provider=hub,
        )

        self.assertIn("persona culpa agotamiento", [call[0] for call in hub.calls])
        self.assertEqual(result.candidates[0].search_term, "persona culpa agotamiento")

    def test_title_exclusive_never_uses_generic_fallback(self):
        hub = FakeAssetHub({
            "mujer triste": [],
            "mujer triste tristeza": [],
            "triste": [],
        })

        discover_material_candidates(
            policy=title_policy("mi-otra-yo"),
            stock_terms=["mujer triste"],
            asset_hub_provider=hub,
        )

        self.assertTrue(all(call[1] == {"sources": [{"scope": "title", "title": "mi-otra-yo"}]} for call in hub.calls))

    def test_title_preferred_skips_generic_when_title_has_enough_candidates(self):
        preferred = MaterialSourcePolicy(
            MaterialProviderPolicy((PROVIDER_ASSET_HUB,)),
            AssetHubCatalogPolicy(include=AssetHubIncludePolicy(generic=True, titles=("mi-otra-yo",))),
        )
        title_assets = [
            {"asset_uid": f"title-{index}", "orientation": "vertical-9x16"}
            for index in range(TITLE_PREFERRED_MIN_CANDIDATES)
        ]
        hub = FakeAssetHub({"mujer triste": title_assets})

        result = discover_material_candidates(
            policy=preferred,
            stock_terms=["mujer triste"],
            asset_hub_provider=hub,
        )

        self.assertEqual(len(result.candidates), TITLE_PREFERRED_MIN_CANDIDATES)
        self.assertTrue(all(call[1] == {"sources": [{"scope": "title", "title": "mi-otra-yo"}]} for call in hub.calls))

    def test_title_preferred_uses_generic_when_title_lacks_candidates(self):
        preferred = MaterialSourcePolicy(
            MaterialProviderPolicy((PROVIDER_ASSET_HUB,)),
            AssetHubCatalogPolicy(include=AssetHubIncludePolicy(generic=True, titles=("mi-otra-yo",))),
        )
        hub = FakeAssetHub({
            "mujer triste": [{"asset_uid": "title-1", "orientation": "vertical-9x16"}],
            "mujer triste tristeza": [{"asset_uid": "generic-1", "orientation": "vertical-9x16"}],
        })

        result = discover_material_candidates(
            policy=preferred,
            stock_terms=["mujer triste"],
            asset_hub_provider=hub,
        )

        self.assertIn({"sources": [{"scope": "generic"}]}, [call[1] for call in hub.calls])
        self.assertEqual([item.canonical_id for item in result.candidates], ["title-1", "generic-1"])

    def test_title_preferred_sufficiency_is_calculated_after_strict_vertical_filter(self):
        preferred = MaterialSourcePolicy(
            MaterialProviderPolicy((PROVIDER_ASSET_HUB,)),
            AssetHubCatalogPolicy(include=AssetHubIncludePolicy(generic=True, titles=("mi-otra-yo",))),
        )
        title_assets = [
            {"asset_uid": f"title-horizontal-{index}", "orientation": "horizontal-16x9"}
            for index in range(20)
        ] + [
            {"asset_uid": "title-vertical-1", "orientation": "vertical-9x16"},
            {"asset_uid": "title-vertical-2", "orientation": "vertical-4x5"},
        ]
        hub = FakeAssetHub({"mujer triste": title_assets})

        result = discover_material_candidates(
            policy=preferred,
            stock_terms=["mujer triste"],
            video_aspect="9:16",
            asset_hub_provider=hub,
        )

        self.assertIn({"sources": [{"scope": "generic"}]}, [call[1] for call in hub.calls])
        self.assertEqual(
            set(item.canonical_id for item in result.candidates),
            {"title-vertical-1", "title-vertical-2"},
        )

    def test_title_exclusive_does_not_generic_fallback_after_strict_vertical_filter(self):
        hub = FakeAssetHub({
            "mujer triste": [
                {"asset_uid": "title-horizontal", "orientation": "horizontal-16x9"},
                {"asset_uid": "title-vertical", "orientation": "vertical-9x16"},
            ],
            "mujer triste tristeza": [],
            "triste": [],
        })

        result = discover_material_candidates(
            policy=title_policy("mi-otra-yo"),
            stock_terms=["mujer triste"],
            video_aspect="9:16",
            asset_hub_provider=hub,
        )

        self.assertEqual([item.canonical_id for item in result.candidates], ["title-vertical"])
        self.assertTrue(all(call[1] == {"sources": [{"scope": "title", "title": "mi-otra-yo"}]} for call in hub.calls))

    def test_human_review_reserve_uses_visual_query_v2(self):
        hub = FakeAssetHub({
            "mujer triste": [{"asset_uid": "a", "orientation": "vertical-9x16"}],
            "mujer triste tristeza": [{"asset_uid": "b", "orientation": "vertical-9x16"}],
        })

        result = discover_asset_hub_review_reserve_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            terms=["mujer triste"],
            asset_hub_provider=hub,
        )

        self.assertIn("mujer triste tristeza", [call[0] for call in hub.calls])
        self.assertEqual([item.canonical_id for item in result.candidates], ["a", "b"])

    def test_human_review_reserve_excludes_horizontal_assets_for_vertical_output(self):
        hub = FakeAssetHub({
            "mujer triste": [
                {"asset_uid": "h", "orientation": "horizontal-16x9"},
                {"asset_uid": "v", "orientation": "vertical-9x16"},
            ],
            "mujer triste tristeza": [],
            "triste": [],
        })

        result = discover_asset_hub_review_reserve_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            terms=["mujer triste"],
            video_aspect="9:16",
            asset_hub_provider=hub,
        )

        self.assertEqual([item.canonical_id for item in result.candidates], ["v"])

    def test_human_review_reserve_caches_equivalent_queries_within_one_job(self):
        hub = FakeAssetHub({"shared": [{"asset_uid": "v", "orientation": "vertical-9x16"}]})
        with patch("app.custom.material_discovery._asset_hub_search_queries", return_value=("shared",)):
            discover_asset_hub_review_reserve_candidates(
                policy=policy(PROVIDER_ASSET_HUB), terms=["scene one", "scene two"], asset_hub_provider=hub,
            )
        self.assertEqual([call[0] for call in hub.calls], ["shared"])

    def test_human_review_reserve_opens_circuit_on_unavailable_provider(self):
        hub = FakeAssetHub(error=KurukinAssetHubUnavailableError("offline"))
        result = discover_asset_hub_review_reserve_candidates(
            policy=policy(PROVIDER_ASSET_HUB), terms=["uno", "dos"], asset_hub_provider=hub,
        )
        self.assertEqual(len(hub.calls), 1)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.diagnostics[0].status, "unavailable")
        self.assertEqual(result.diagnostics[0].candidate_count, 0)

    def test_asset_hub_unavailable_keeps_pexels_and_pixabay_candidates(self):
        hub = FakeAssetHub(error=KurukinAssetHubUnavailableError("timeout"))
        with patch("app.custom.material_discovery.material.search_videos_for_provider", side_effect=lambda provider, *_args: [stock(provider, provider)]):
            result = discover_material_candidates(
                policy=policy(PROVIDER_ASSET_HUB, PROVIDER_PEXELS, PROVIDER_PIXABAY),
                stock_terms=["term"],
                asset_hub_provider=hub,
            )

        self.assertEqual({item.provider for item in result.candidates}, {PROVIDER_PEXELS, PROVIDER_PIXABAY})
        diagnostics = provider_diagnostics_for_review(
            result,
            enabled_providers=(PROVIDER_ASSET_HUB, PROVIDER_PEXELS, PROVIDER_PIXABAY),
        )
        asset_hub = diagnostics[0]
        self.assertEqual(asset_hub["status"], "unavailable")
        self.assertEqual(asset_hub["error_class"], "KurukinAssetHubUnavailableError")
        self.assertEqual(asset_hub["candidate_count"], 0)

    def test_asset_hub_outage_discards_prior_reserve_candidates(self):
        class Hub:
            def __init__(self):
                self.calls = 0

            def search(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return [{"asset_uid": "prior", "orientation": "vertical-9x16"}]
                raise KurukinAssetHubUnavailableError("circuit open")

        result = discover_asset_hub_review_reserve_candidates(
            policy=policy(PROVIDER_ASSET_HUB), terms=["first", "second"],
            asset_hub_provider=Hub(), queries_are_visual=True,
        )
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.diagnostics[-1].status, "unavailable")

    def test_asset_hub_validation_error_remains_fatal(self):
        with self.assertRaises(KurukinAssetHubValidationError):
            discover_asset_hub_review_reserve_candidates(
                policy=policy(PROVIDER_ASSET_HUB), terms=["term"],
                asset_hub_provider=FakeAssetHub(error=KurukinAssetHubValidationError("malformed response")),
            )

    def test_human_review_reserve_does_not_reexpand_v2_visual_queries(self):
        hub = FakeAssetHub({"mujer triste tristeza": [{"asset_uid": "v", "orientation": "vertical-9x16"}]})
        result = discover_asset_hub_review_reserve_candidates(
            policy=policy(PROVIDER_ASSET_HUB),
            terms=["mujer triste tristeza"],
            asset_hub_provider=hub,
            queries_are_visual=True,
        )
        self.assertEqual([call[0] for call in hub.calls], ["mujer triste tristeza"])
        self.assertEqual([item.canonical_id for item in result.candidates], ["v"])

    def test_scale_fixture_reports_human_review_request_budget(self):
        """10 canonical scenes: old V1 was 3 queries x 3 tries per scene."""
        terms = [f"visual scene {index}" for index in range(10)]
        hub = FakeAssetHub({term: [] for term in terms})
        discover_asset_hub_review_reserve_candidates(
            policy=policy(PROVIDER_ASSET_HUB), terms=terms,
            asset_hub_provider=hub, queries_are_visual=True,
        )
        report = {
            "before_request_attempts": len(terms) * 3 * 3,
            "before_timeout_seconds": len(terms) * 3 * 3 * 15,
            "after_request_calls": len(hub.calls),
            # The circuit opens on the first unavailable request.
            "after_unavailable_timeout_seconds": 15,
        }
        self.assertEqual(report["after_request_calls"], 10)
        self.assertEqual(report["before_request_attempts"], 90)
        self.assertEqual(report["after_unavailable_timeout_seconds"], 15)

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

    def test_provider_diagnostics_are_aggregated_and_sanitized(self):
        def search(provider, *_args):
            if provider == "pexels":
                raise RuntimeError("503 api_key=secret")
            return [stock(provider, provider)]
        with patch("app.custom.material_discovery.material.search_videos_for_provider", search, create=True):
            result = discover_material_candidates(
                policy=policy(PROVIDER_PEXELS, PROVIDER_PIXABAY), stock_terms=["term"]
            )
        diagnostics = provider_diagnostics_for_review(
            result, enabled_providers=(PROVIDER_PEXELS, PROVIDER_PIXABAY)
        )
        by_provider = {item["provider"]: item for item in diagnostics}
        self.assertEqual(by_provider["pexels"]["status"], "error")
        self.assertTrue(by_provider["pexels"]["attempted"])
        self.assertNotIn("secret", str(by_provider["pexels"]).lower())
        self.assertEqual(by_provider["pixabay"]["deduped_count"], 1)

    def test_missing_native_configuration_is_reported_without_calling_mpt(self):
        with patch("app.custom.material_discovery.native_stock_provider_configured", return_value=False), \
             patch("app.custom.material_discovery.material.search_videos_for_provider") as search:
            result = discover_material_candidates(
                policy=policy(PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR),
                stock_terms=["woman worried at home"],
            )

        search.assert_not_called()
        self.assertEqual([item.status for item in result.diagnostics], [
            "config_missing", "config_missing", "config_missing",
        ])
        diagnostics = provider_diagnostics_for_review(
            result,
            enabled_providers=(PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR),
        )
        self.assertEqual({item["status"] for item in diagnostics}, {"config_missing"})

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
        # Keep this independent from any real fixture or local asset in the
        # workspace.  Discovery delegates only to the allow-listed picker; an
        # explicit empty response models an empty safe local catalog.
        with patch("app.custom.material_discovery.pick_local_visual_for_intent", return_value=None) as picker:
            result = discover_material_candidates(policy=policy(PROVIDER_LOCAL), stock_terms=["term"])
        picker.assert_called_once_with({"topic": "term", "visual_keywords": ["term"]})
        self.assertEqual(result.diagnostics[0].status, "empty")


if __name__ == "__main__":
    unittest.main()
