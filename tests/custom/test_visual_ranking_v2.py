import math
import importlib.util
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from app.custom.candidate_ranking_v2 import (
    WEIGHTS,
    candidate_identity_keys,
    evaluate_candidate_eligibility,
    normalize_source_url,
    rank_candidate,
    rank_candidates_v2,
    stable_secondary_dedupe,
)
from app.custom.material_discovery import MaterialCandidate, MaterialDiscoveryResult
from app.custom.material_selection import select_material_candidates
from app.custom.scene_visual_intent import build_scene_visual_intent, build_scene_retrieval_queries
from app.custom import human_review


_BENCHMARK_SPEC = importlib.util.spec_from_file_location(
    "benchmark_visual_ranking",
    Path(__file__).resolve().parents[2] / "scripts/benchmark_visual_ranking.py",
)
benchmark_visual_ranking = importlib.util.module_from_spec(_BENCHMARK_SPEC)
assert _BENCHMARK_SPEC and _BENCHMARK_SPEC.loader
_BENCHMARK_SPEC.loader.exec_module(benchmark_visual_ranking)


def asset(uid, description, **metadata):
    return MaterialCandidate(
        "asset_hub", uid, uid, "woman resting", rank=1, width=metadata.pop("width", 1080),
        height=metadata.pop("height", 1920), duration=metadata.pop("duration", 5),
        orientation=metadata.pop("orientation", "portrait"), source_info={"visual_description": description, **metadata},
    )


class TestVisualRankingV2(unittest.TestCase):
    def test_offline_benchmark_uses_same_raw_pool_and_real_entry_points(self):
        scenarios = benchmark_visual_ranking.load_fixture()
        with patch.object(
            benchmark_visual_ranking.material_selection, "select_material_candidates",
            wraps=select_material_candidates,
        ) as v1, patch.object(
            benchmark_visual_ranking.candidate_ranking_v2, "rank_candidates_v2",
            wraps=rank_candidates_v2,
        ) as v2:
            report = benchmark_visual_ranking.run_benchmark(scenarios)

        self.assertEqual(v1.call_count, len(scenarios))
        self.assertEqual(v2.call_count, len(scenarios))
        for scenario, result, v1_call, v2_call in zip(scenarios, report["scenarios"], v1.call_args_list, v2.call_args_list):
            fixture_ids = [item["asset_uid"] for item in scenario["candidates"]]
            self.assertEqual(result["input_candidate_ids"], fixture_ids)
            self.assertEqual(result["v1_input_candidate_ids"], fixture_ids)
            self.assertEqual(result["v2_input_candidate_ids"], fixture_ids)
            self.assertEqual([item.canonical_id for item in v1_call.kwargs["discovery_result"].candidates], fixture_ids)
            self.assertEqual([item.canonical_id for item in v2_call.args[1]], fixture_ids)

    def test_offline_benchmark_is_deterministic_and_never_opens_network(self):
        with patch.object(socket, "create_connection", side_effect=AssertionError("network is forbidden")):
            first = benchmark_visual_ranking.run_benchmark()
            second = benchmark_visual_ranking.run_benchmark()
        self.assertEqual(first, second)

    def test_benchmark_top_three_and_v2_secondary_dedupe_use_real_results(self):
        scenario = next(item for item in benchmark_visual_ranking.load_fixture() if item["id"] == "guilt_rest")
        report = benchmark_visual_ranking.run_benchmark((scenario,))["scenarios"][0]
        pool = benchmark_visual_ranking.pool_for_scenario(scenario)
        v1 = select_material_candidates(
            discovery_result=MaterialDiscoveryResult(pool, (), ("asset_hub",), ("asset_hub",), {}),
            video_aspect="9:16", target_duration=15, clip_duration=5,
        )
        raw_v2 = rank_candidates_v2(build_scene_visual_intent(scenario["scene"]), pool, video_aspect="9:16", clip_duration=5)
        deduped = stable_secondary_dedupe([candidate for candidate, _ranking in raw_v2])
        self.assertEqual([item["asset_uid"] for item in report["v1"]["top3"]], [d.candidate.canonical_id for d in v1.decisions[:3]])
        self.assertEqual([item["asset_uid"] for item in report["v2"]["top3"]], [candidate.canonical_id for candidate in deduped[:3]])
        self.assertEqual(report["duplicate_removed_count"], len(raw_v2) - len(deduped))
        self.assertGreater(report["duplicate_removed_count"], 0)

    def test_benchmark_expected_editorial_preference_is_manual_fixture_data(self):
        scenarios = benchmark_visual_ranking.load_fixture()
        expected = {
            "guilt_rest": "guilt-reflective-home", "exhaustion": "exhaustion-kitchen",
            "anxiety": "anxiety-doorway", "abandonment": "abandonment-window",
            "family_conflict": "family-conflict-table", "loneliness": "loneliness-bedroom",
            "reconciliation": "reconciliation-embrace", "literal_simple": "literal-reading-letter",
        }
        self.assertEqual({item["id"]: item["expected_editorial_preference"] for item in scenarios}, expected)
        self.assertNotIn("expected_editorial_preference", benchmark_visual_ranking.run_v2_real.__code__.co_consts)

    def test_intent_is_deterministic_serializable_and_has_negatives(self):
        first = build_scene_visual_intent("Una mujer se siente culpable cuando descansa.", editorial_profile={"subject_gender": "feminine"})
        second = build_scene_visual_intent("Una mujer se siente culpable cuando descansa.", editorial_profile={"subject_gender": "feminine"})
        self.assertEqual(first, second)
        self.assertIn("guilt", first.emotional_intent)
        self.assertIn("smiling at camera", first.negative_concepts)
        self.assertEqual(set(first.to_dict()), {"literal_concepts", "emotional_intent", "character_state", "relationship_context", "action", "environment", "cinematic_mood", "shot_preferences", "negative_concepts", "visual_motif", "temporal_context"})

    def test_mood_shift_aliases_are_emotional_state_not_comedy(self):
        for phrase in ("cambio de humor", "cambios de humor", "cambie de humor", "mal humor", "estado de ánimo", "cambio de ánimo"):
            with self.subTest(phrase=phrase):
                intent = build_scene_visual_intent(f"ella nota un {phrase}")
                self.assertIn("interpersonal tension", intent.emotional_intent)
                self.assertNotIn("comedy", " ".join(intent.to_dict().values().__str__()))

    def test_provider_queries_preserve_observable_relationship_intent(self):
        intent = build_scene_visual_intent("una mujer observa cambios de humor de su pareja en casa")
        stock = build_scene_retrieval_queries(intent, "pexels")
        hub = build_scene_retrieval_queries(intent, "asset_hub")
        self.assertTrue(stock and hub)
        self.assertTrue(all("persona amable" not in item and item != "persona" for item in stock))
        self.assertTrue(any("watching" in item or "two people" in item for item in stock))
        self.assertTrue(any("observando" in item or "dos personas" in item for item in hub))
        self.assertTrue(all(item.isascii() for item in stock))

    def test_stock_queries_keep_a_concrete_video_term_seed_and_scene_context(self):
        intent = build_scene_visual_intent("una persona agotada descansa en casa")
        queries = build_scene_retrieval_queries(intent, "pixabay", ("worried person", "abstract emotional pattern"))
        self.assertIn("worried person", queries)
        self.assertTrue(any("tired" in query or "home" in query for query in queries))
        self.assertTrue(all(query.isascii() and len(query.split()) <= 7 for query in queries))

    def test_explicit_female_protagonist_is_inherited_by_neutral_segments(self):
        queries = human_review.retrieval_queries_for_review_segments(
            "Te sientas. Intentas relajarte.", 2,
            material_title="La mujer que se siente culpable cuando descansa",
        )
        self.assertTrue(all("woman" in scene["pexels"][0] for scene in queries))

    def test_explicit_male_protagonist_is_inherited_by_neutral_segments(self):
        queries = human_review.retrieval_queries_for_review_segments(
            "Se sienta. Intenta relajarse.", 2,
            material_title="El hombre que no puede descansar",
        )
        self.assertTrue(all("man" in scene["pixabay"][0] for scene in queries))

    def test_no_explicit_subject_remains_person_neutral(self):
        intent = build_scene_visual_intent("Intenta relajarse en casa")
        self.assertNotIn("woman", intent.literal_concepts)
        self.assertNotIn("man", intent.literal_concepts)
        self.assertIn("person", build_scene_retrieval_queries(intent, "pexels")[0])

    def test_local_explicit_subject_overrides_global_subject(self):
        intent = build_scene_visual_intent(
            "Un hombre intenta relajarse en casa", inherited_subject="woman",
        )
        self.assertEqual(intent.literal_concepts[0], "man")
        self.assertIn("man", build_scene_retrieval_queries(intent, "pexels")[0])

    def test_video_term_subject_hint_cannot_override_explicit_script_subject(self):
        intent = build_scene_visual_intent(
            "Un hombre intenta relajarse en casa", subject_hints=("woman resting at home",),
        )
        self.assertEqual(intent.literal_concepts[0], "man")

    def test_guilt_while_resting_prefers_narrative_candidate_over_commercial_literal(self):
        intent = build_scene_visual_intent("una mujer se siente culpable cuando descansa", editorial_profile={"subject_gender": "feminine"})
        literal = asset("literal", "woman smiling at camera on sofa, commercial wellness advertisement", contains_people=True, person_visibility="clear", visual_presentation="feminine")
        narrative = asset("narrative", "tired woman sitting alone at home, reflective and anxious", height=1350, contains_people=True, person_visibility="clear", visual_presentation="natural", action_description="sitting thoughtfully")
        ranked = rank_candidates_v2(intent, [literal, narrative], video_aspect="9:16", clip_duration=5)
        self.assertEqual(ranked[0][0].canonical_id, "narrative")
        self.assertIn("negative_commercial_aesthetic", ranked[1][1].penalty_codes)

    def test_scores_are_bounded_and_sequence_is_unavailable(self):
        intent = build_scene_visual_intent("soledad y ansiedad en casa")
        candidate = asset("one", "worried woman alone at home", contains_people=True)
        ranking = rank_candidate(intent, candidate, video_aspect="9:16", clip_duration=5, previous_candidates=[candidate])
        self.assertNotIn("sequence_adjustment", ranking.score_components)
        self.assertTrue(all(0 <= value <= 1 for value in ranking.score_components.values()))
        self.assertTrue(0 <= ranking.total_score <= 1)

    def test_missing_metadata_is_safe(self):
        intent = build_scene_visual_intent("escena simple")
        ranking = rank_candidate(intent, asset("minimal", ""), video_aspect="9:16", clip_duration=5)
        self.assertTrue(0 <= ranking.total_score <= 1)

    def test_eligibility_uses_provider_neutral_materializable_identity(self):
        cases = (
            (MaterialCandidate("asset_hub", "", "", "term"), "missing_canonical_identity"),
            (asset("blank", "", media_type="image"), "non_video_media"),
            (asset("denied", "", rights_state="denied"), "explicitly_not_production_eligible"),
            (asset("not-eligible", "", production_eligible=False), "explicitly_not_production_eligible"),
        )
        for candidate, code in cases:
            with self.subTest(code=code):
                eligibility = evaluate_candidate_eligibility(candidate)
                self.assertFalse(eligibility.eligible)
                self.assertIn(code, eligibility.rejection_codes)
        self.assertTrue(evaluate_candidate_eligibility(asset("minimal-video", "")).eligible)
        self.assertTrue(evaluate_candidate_eligibility(MaterialCandidate("pexels", "pexels:7", "pexels:7", "term")).eligible)

    def test_mixed_provider_emotional_scene_beats_keyword_match_without_rich_metadata(self):
        intent = build_scene_visual_intent("ansiedad por el humor de otra persona en casa")
        gamer = MaterialCandidate("asset_hub", "hub:gamer", "hub:gamer", "ansiedad", width=1080, height=1920, duration=5,
                                  source_info={"visual_description": "gamer streaming looking at camera, commercial influencer pose", "media_type": "video"})
        concerned = MaterialCandidate("pexels", "pexels:concerned", "pexels:concerned", "ansiedad", width=1080, height=1920, duration=5,
                                      source_info={"title": "Concerned person at home", "description": "worried person observing another person's reaction in a quiet home", "media_type": "video"})
        ranked = rank_candidates_v2(intent, [gamer, concerned], video_aspect="9:16", clip_duration=5)
        self.assertEqual(ranked[0][0].canonical_id, "pexels:concerned")
        self.assertIn("negative_gaming", ranked[1][1].penalty_codes)
        self.assertNotIn("cinematic_editorial", ranked[0][1].score_components)

    def test_explicit_spanish_alias_contradiction_cannot_beat_relational_candidate(self):
        intent = build_scene_visual_intent("mujer con ansiedad observando el cambio de ánimo de su pareja")
        celebration = asset("fiesta", "cumpleaños y fiesta, mujer sonriendo en publicidad comercial", editorial_quality=99, contains_people=True)
        relational = asset("tension", "worried woman watching another person's reaction during tense conversation at home", contains_people=True)
        ranked = rank_candidates_v2(intent, [celebration, relational], video_aspect="9:16", clip_duration=5)
        self.assertEqual(ranked[0][0].canonical_id, "tension")
        self.assertIn("explicit_narrative_contradiction", ranked[-1][1].penalty_codes)

    def test_limited_metadata_is_unknown_not_a_contradiction(self):
        intent = build_scene_visual_intent("culpa y ansiedad al descansar")
        minimal = asset("limited", "", contains_people=True)
        ranking = rank_candidate(intent, minimal, video_aspect="9:16", clip_duration=5)
        self.assertNotIn("explicit_narrative_contradiction", ranking.penalty_codes)

    def test_zero_editorial_unknown_cannot_beat_positive_match_on_technical_score(self):
        intent = build_scene_visual_intent("mujer con culpa al descansar en casa")
        unknown = MaterialCandidate(
            "asset_hub", "hub:unknown", "hub:unknown", "generic stock clip", width=2160, height=3840,
            duration=30, orientation="portrait", source_info={"editorial_quality": 100, "contains_people": True},
        )
        match = MaterialCandidate(
            "pexels", "pexels:match", "pexels:match", "resting", width=720, height=1280,
            duration=5, orientation="portrait", source_info={"description": "worried woman resting alone at home"},
        )
        ranked = rank_candidates_v2(intent, [unknown, match], video_aspect="9:16", clip_duration=5)
        self.assertEqual(ranked[0][0].canonical_id, "pexels:match")
        self.assertNotIn("explicit_narrative_contradiction", ranked[1][1].penalty_codes)

    def test_asset_specific_source_page_is_editorial_evidence_not_retrieval_query(self):
        intent = build_scene_visual_intent("una mujer busca reconciliarse después de un conflicto")
        object_clip = MaterialCandidate(
            "pexels", "pexels:object", "pexels:object", "woman reconciliation",
            width=1080, height=1920, duration=10,
            source_info={"source_page": "https://www.pexels.com/video/skateboards-leaning-on-the-wall-1/"},
        )
        relationship = MaterialCandidate(
            "pexels", "pexels:relationship", "pexels:relationship", "woman reconciliation",
            width=720, height=1280, duration=5,
            source_info={"source_page": "https://www.pexels.com/video/woman-embracing-her-sister-2/"},
        )

        ranked = rank_candidates_v2(intent, [object_clip, relationship], video_aspect="9:16", clip_duration=5)

        self.assertEqual(ranked[0][0].canonical_id, "pexels:relationship")
        self.assertEqual(ranked[1][0].canonical_id, "pexels:object")
        self.assertNotIn("explicit_narrative_contradiction", ranked[1][1].penalty_codes)

    def test_provider_is_not_a_preference_in_a_common_ranking(self):
        intent = build_scene_visual_intent("reconciliación después de un conflicto familiar")
        pool = [
            MaterialCandidate("asset_hub", "hub:office", "hub:office", "reconciliación", source_info={"visual_description": "corporate handshake in office", "media_type": "video"}),
            MaterialCandidate("pexels", "pexels:embrace", "pexels:embrace", "reconciliación", source_info={"description": "family members embracing warmly after conflict", "media_type": "video"}),
            MaterialCandidate("pixabay", "pixabay:party", "pixabay:party", "reconciliación", source_info={"description": "party celebration", "media_type": "video"}),
            MaterialCandidate("coverr", "coverr:talk", "coverr:talk", "reconciliación", source_info={"description": "two people talking quietly at home", "media_type": "video"}),
        ]
        ranked = rank_candidates_v2(intent, pool, video_aspect="9:16", clip_duration=5)
        self.assertEqual(ranked[0][0].canonical_id, "pexels:embrace")

    def test_invalid_optional_numbers_are_unavailable_not_crashes(self):
        intent = build_scene_visual_intent("escena simple")
        for value in (None, "", "unknown", -1, 0, float("nan"), float("inf")):
            with self.subTest(duration=value):
                candidate = asset("duration", "scene", duration=value, height=None)
                ranking = rank_candidate(intent, candidate, video_aspect="9:16", clip_duration=5)
                self.assertEqual(ranking.score_components["technical_usability"], round(34 / 44, 4))
        for value in (None, "", "unknown", -1, 0, float("nan"), float("inf")):
            with self.subTest(width=value):
                candidate = asset("width", "scene", duration=None, height=value, orientation=None)
                ranking = rank_candidate(intent, candidate, video_aspect="9:16", clip_duration=5)
                self.assertNotIn("technical_usability", ranking.score_components)

    def test_technical_score_renormalizes_and_missing_duration_has_no_freebie(self):
        intent = build_scene_visual_intent("escena simple")
        missing = asset("missing", "scene", duration=None, width=1080, height=1920)
        bad = asset("bad", "scene", duration=1, width=1080, height=1920)
        missing_score = rank_candidate(intent, missing, video_aspect="9:16", clip_duration=5).score_components["technical_usability"]
        bad_score = rank_candidate(intent, bad, video_aspect="9:16", clip_duration=5).score_components["technical_usability"]
        orientation = 34 / 44
        resolution = 1.0
        self.assertEqual(missing_score, round((.45 * orientation + .25 * resolution) / .70, 4))
        self.assertGreater(missing_score, bad_score)

    def test_unavailable_components_are_excluded_from_exact_final_formula(self):
        intent = build_scene_visual_intent("escena simple")
        candidate = asset("formula", "scene", duration=None, width=None, height=None, orientation=None)
        ranking = rank_candidate(intent, candidate, video_aspect="9:16", clip_duration=5)
        self.assertNotIn("technical_usability", ranking.score_components)
        self.assertNotIn("subtitle_overlay_safety", ranking.score_components)
        self.assertNotIn("provenance_confidence", ranking.score_components)
        expected = sum(WEIGHTS[key] * value for key, value in ranking.score_components.items()) / sum(WEIGHTS[key] for key in ranking.score_components)
        self.assertEqual(ranking.total_score, round(expected, 4))
        self.assertTrue(math.isfinite(ranking.total_score))

    def test_ineligible_candidates_are_not_ranked(self):
        intent = build_scene_visual_intent("mujer en casa")
        rejected = asset("rejected", "woman", media_type="image")
        accepted = asset("accepted", "woman")
        ranked = rank_candidates_v2(intent, [rejected, accepted], video_aspect="9:16", clip_duration=5)
        self.assertEqual([candidate.canonical_id for candidate, _ranking in ranked], ["accepted"])

    def test_secondary_identity_dedupe_keys_and_missing_values(self):
        same_uid = [asset("same", "one"), asset("same", "two")]
        same_provider_asset = [
            asset("one", "one", provider_asset_id="provider-7"),
            asset("two", "two", provider_asset_id="provider-7"),
        ]
        different_provider = [
            MaterialCandidate("asset_hub", "one", "one", "term", source_info={"provider_asset_id": "provider-7"}),
            MaterialCandidate("other_hub", "two", "two", "term", source_info={"provider_asset_id": "provider-7"}),
        ]
        same_source_identity = [
            asset("one", "one", source_identity="Source-7"),
            asset("two", "two", source_identity="source-7"),
        ]
        missing_secondary = [asset("one", "one"), asset("two", "two")]

        self.assertEqual(len(stable_secondary_dedupe(same_uid)), 1)
        self.assertEqual(len(stable_secondary_dedupe(same_provider_asset)), 1)
        self.assertEqual(len(stable_secondary_dedupe(different_provider)), 2)
        self.assertEqual(len(stable_secondary_dedupe(same_source_identity)), 1)
        self.assertEqual(len(stable_secondary_dedupe(missing_secondary)), 2)
        self.assertNotIn("source_identity:", " ".join(candidate_identity_keys(missing_secondary[0])))

    def test_source_url_dedupe_is_safe_and_stable(self):
        first = asset("first", "one", source_url=" HTTPS://Example.TEST/a.mp4?clip=1#poster ")
        variant = asset("variant", "two", source_url="https://example.test/a.mp4?clip=1#different")
        distinct_query = asset("distinct", "three", source_url="https://example.test/a.mp4?clip=2")
        missing = asset("missing", "four")

        self.assertEqual(
            normalize_source_url(" HTTPS://Example.TEST/a.mp4?clip=1#poster "),
            "https://example.test/a.mp4?clip=1",
        )
        self.assertEqual(
            [item.canonical_id for item in stable_secondary_dedupe([first, variant, distinct_query, missing])],
            ["first", "distinct", "missing"],
        )

    def test_secondary_dedupe_keeps_first_ranked_candidate(self):
        best = asset("best", "best", provider_asset_id="source-1")
        duplicate = asset("duplicate", "duplicate", provider_asset_id="source-1")
        different = asset("different", "different", provider_asset_id="source-2")
        self.assertEqual(
            [item.canonical_id for item in stable_secondary_dedupe([best, duplicate, different])],
            ["best", "different"],
        )


if __name__ == "__main__":
    unittest.main()
