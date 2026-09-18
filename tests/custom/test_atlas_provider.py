import unittest
from uuid import uuid4

from app.custom.atlas_client import AtlasInvalidRequestError, AtlasProtocolError
from app.custom.atlas_provider import AtlasProvider, build_atlas_search_payload
from app.custom.material_selection import _is_orientation_compatible
from app.models.schema import VideoAspect


UID = str(uuid4())


def atlas_candidate(*, uid=UID, kind="horizontal", content=None, thumbnail=None, score=0.91):
    return {
        "asset_uid": uid,
        "producer": "producer-a",
        "source_provenance": {"source_kind": "catalog", "source_key": "safe-key", "producer_asset_id": "a-1"},
        "catalog_placement": {"catalog_scope": "title", "title_id": "title-1", "brand_id": None},
        "selected_rendition": {
            "kind": kind,
            "content_locator": content or f"/v1/assets/{uid}/renditions/{kind}/content",
            "thumbnail_locator": thumbnail if thumbnail is not None else f"/v1/assets/{uid}/renditions/{kind}/thumbnail",
        },
        "evidence": {"visual_summary": "safe editorial evidence", "keywords": []},
        "requirement_matches": {},
        "contradictions": [],
        "confidence": "high",
        "score_components": {"lexical_relevance": .2, "structured_match": .3, "rendition_preference": .2, "editorial_signal": .2},
        "atlas_local_score": score,
        "remote_path": "rclone://should-not-be-copied",
    }


class FakeClient:
    def __init__(self, candidates):
        self.candidates = candidates
        self.payload = None

    def search(self, payload):
        self.payload = payload
        return {"schema_version": "asset_candidates_v1", "candidates": self.candidates, "scarcity_reason": None}


class TestAtlasProvider(unittest.TestCase):
    def test_explicit_atlas_scope_is_required(self):
        with self.assertRaises(AtlasInvalidRequestError):
            build_atlas_search_payload({"query": "city", "scope": {"kind": "general"}})

    def test_supported_scopes_remain_exact(self):
        cases = (
            ({"kind": "title", "title_id": "t1"}, {"kind": "title", "title_id": "t1"}),
            ({"kind": "general"}, {"kind": "general"}),
            ({"kind": "catalog"}, {"kind": "catalog"}),
        )
        for scope, expected in cases:
            with self.subTest(scope=scope):
                self.assertEqual(build_atlas_search_payload({"atlas_scope": scope})["scope"], expected)

    def test_explicit_portrait_orientation_requires_vertical_rendition(self):
        payload = build_atlas_search_payload({"atlas_scope": {"kind": "general"}, "aspect_ratio": "9:16"})

        self.assertEqual(payload["preferences"]["preferred_rendition_kind"], "vertical")
        self.assertEqual(payload["requirements"]["rendition"], {
            "acceptable_kinds": ["horizontal", "vertical"],
            "preferred_kind": "vertical",
            "preferred_required": True,
        })

    def test_explicit_landscape_orientation_requires_horizontal_rendition(self):
        payload = build_atlas_search_payload({"atlas_scope": {"kind": "general"}, "aspect_ratio": "16:9"})

        self.assertEqual(payload["preferences"]["preferred_rendition_kind"], "horizontal")
        self.assertEqual(payload["requirements"]["rendition"], {
            "acceptable_kinds": ["horizontal", "vertical"],
            "preferred_kind": "horizontal",
            "preferred_required": True,
        })

    def test_video_aspect_portrait_requires_vertical_rendition(self):
        client = FakeClient([])
        AtlasProvider(client).search({
            "atlas_scope": {"kind": "general"}, "aspect_ratio": VideoAspect.portrait,
        })

        self.assertEqual(client.payload["requirements"]["rendition"], {
            "acceptable_kinds": ["horizontal", "vertical"],
            "preferred_kind": "vertical",
            "preferred_required": True,
        })

    def test_video_aspect_landscape_requires_horizontal_rendition(self):
        client = FakeClient([])
        AtlasProvider(client).search({
            "atlas_scope": {"kind": "general"}, "aspect_ratio": VideoAspect.landscape,
        })

        self.assertEqual(client.payload["requirements"]["rendition"], {
            "acceptable_kinds": ["horizontal", "vertical"],
            "preferred_kind": "horizontal",
            "preferred_required": True,
        })

    def test_no_explicit_orientation_preserves_soft_neutral_rendition_request(self):
        landscape = build_atlas_search_payload({"atlas_scope": {"kind": "general"}, "orientation": "landscape"})
        square = build_atlas_search_payload({"atlas_scope": {"kind": "general"}, "orientation": "square", "scene_visual_intent": {"relationships": ["family"]}})
        self.assertEqual(landscape["preferences"]["preferred_rendition_kind"], "horizontal")
        self.assertTrue(landscape["requirements"]["rendition"]["preferred_required"])
        self.assertIsNone(square["preferences"]["preferred_rendition_kind"])
        self.assertEqual(set(square["requirements"]), {"rendition"})
        self.assertEqual(square["requirements"]["rendition"], {
            "acceptable_kinds": ["horizontal", "vertical"],
            "preferred_kind": None,
            "preferred_required": False,
        })

    def test_candidate_identity_and_ordinal_rank_preserve_local_score_as_metadata(self):
        client = FakeClient([atlas_candidate(score=99.9), atlas_candidate(uid=str(uuid4()), kind="vertical", score=-4)])
        found = AtlasProvider(client).search({"query": "conversation", "atlas_scope": {"kind": "title", "title_id": "t1"}})
        self.assertEqual(found[0].provider, "atlas")
        self.assertEqual(found[0].canonical_id, f"{UID}:horizontal")
        self.assertEqual(found[0].dedupe_key, f"atlas:{UID}:horizontal")
        self.assertEqual([item.rank for item in found], [1, 2])
        self.assertEqual(found[0].orientation, "landscape")
        self.assertEqual(found[1].orientation, "portrait")
        self.assertNotIn(found[0].orientation, {"horizontal", "vertical"})
        self.assertNotIn(found[1].orientation, {"horizontal", "vertical"})
        self.assertEqual(found[0].source_info["rendition_kind"], "horizontal")
        self.assertEqual(found[1].source_info["rendition_kind"], "vertical")
        self.assertEqual(found[0].source_info["atlas_local_score"], 99.9)
        self.assertNotEqual(found[0].rank, found[0].source_info["atlas_local_score"])
        self.assertIsNone(found[0].url)
        self.assertNotIn("remote_path", found[0].source_info)
        self.assertNotIn("rclone", str(found[0].source_info))

    def test_normalized_renditions_survive_only_their_matching_orientation_filter(self):
        vertical_uid = str(uuid4())
        client = FakeClient([
            atlas_candidate(uid=vertical_uid, kind="vertical"),
            atlas_candidate(kind="horizontal"),
        ])

        vertical, horizontal = AtlasProvider(client).search(
            {"query": "conversation", "atlas_scope": {"kind": "title", "title_id": "t1"}}
        )

        self.assertEqual(vertical.source_info["rendition_kind"], "vertical")
        self.assertEqual(vertical.orientation, "portrait")
        self.assertTrue(_is_orientation_compatible(vertical, "9:16"))
        self.assertFalse(_is_orientation_compatible(vertical, "16:9"))
        self.assertEqual(horizontal.source_info["rendition_kind"], "horizontal")
        self.assertEqual(horizontal.orientation, "landscape")
        self.assertTrue(_is_orientation_compatible(horizontal, "16:9"))
        self.assertFalse(_is_orientation_compatible(horizontal, "9:16"))
        self.assertIsNone(vertical.url)
        self.assertEqual(vertical.canonical_id, f"{vertical_uid}:vertical")
        self.assertEqual(vertical.dedupe_key, f"atlas:{vertical_uid}:vertical")

    def test_unsafe_logical_locators_are_rejected(self):
        bad = (
            f"https://elsewhere/v1/assets/{UID}/renditions/horizontal/content",
            f"//elsewhere/v1/assets/{UID}/renditions/horizontal/content",
            f"/v1/assets/{uuid4()}/renditions/horizontal/content",
            f"/v1/assets/{UID}/renditions/vertical/content",
            f"/v1/assets/{UID}/renditions/horizontal/content?x=1",
        )
        for locator in bad:
            with self.subTest(locator=locator), self.assertRaises(AtlasProtocolError):
                AtlasProvider(FakeClient([atlas_candidate(content=locator)])).search({"atlas_scope": {"kind": "general"}})

    def test_limit_defaults_to_20_and_rejects_invalid_explicit_values(self):
        self.assertEqual(build_atlas_search_payload({"atlas_scope": {"kind": "general"}})["limit"], 20)
        for limit in (True, False, 0, -1, 101, 1.5, "20"):
            with self.subTest(limit=limit), self.assertRaises(AtlasInvalidRequestError):
                build_atlas_search_payload({"atlas_scope": {"kind": "general"}, "limit": limit})
        self.assertEqual(build_atlas_search_payload({"atlas_scope": {"kind": "general"}, "limit": 100})["limit"], 100)
