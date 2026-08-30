#!/usr/bin/env python3
"""Offline, read-only V1-real versus V2-real visual-ranking benchmark."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.custom import candidate_ranking_v2, material_selection
from app.custom.material_discovery import MaterialCandidate, MaterialDiscoveryResult
from app.custom.scene_visual_intent import build_scene_visual_intent


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests/custom/fixtures/visual_ranking_benchmark.json"
TOP_N = 3


def load_fixture(path: Path = FIXTURE_PATH) -> tuple[dict[str, Any], ...]:
    """Load static scenarios only; this module never opens a provider or storage."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("benchmark fixture must contain a scenarios list")
    overrides = payload.get("provider_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("provider_overrides must be an object")
    normalized: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        copy = dict(scenario)
        copy["candidates"] = [
            {**candidate, "provider": str(overrides.get(candidate.get("asset_uid"), candidate.get("provider") or "asset_hub"))}
            if isinstance(candidate, dict) else candidate
            for candidate in (scenario.get("candidates") or [])
        ]
        normalized.append(copy)
    return tuple(normalized)


def candidate_from_fixture(item: Mapping[str, Any]) -> MaterialCandidate:
    uid = str(item["asset_uid"])
    source_info = item.get("source_info", {})
    if not isinstance(source_info, dict):
        raise ValueError(f"{uid}: source_info must be an object")
    provider = str(item.get("provider") or "asset_hub")
    return MaterialCandidate(
        provider=provider, canonical_id=uid, dedupe_key=uid,
        search_term=str(item["search_term"]), rank=item.get("rank"),
        duration=item.get("duration"), width=item.get("width"), height=item.get("height"),
        orientation=item.get("orientation"), source_info=dict(source_info),
    )


def pool_for_scenario(scenario: Mapping[str, Any]) -> tuple[MaterialCandidate, ...]:
    candidates = scenario.get("candidates")
    if not isinstance(candidates, list) or len(candidates) < TOP_N:
        raise ValueError(f"{scenario.get('id', '<unknown>')}: at least {TOP_N} candidates are required")
    return tuple(candidate_from_fixture(item) for item in candidates)


def run_v1_real(pool: tuple[MaterialCandidate, ...], *, video_aspect: str, clip_duration: float):
    """Call the autonomous pipeline's real, unmodified V1 function."""
    discovery = MaterialDiscoveryResult(pool, (), ("asset_hub",), ("asset_hub",), {})
    return material_selection.select_material_candidates(
        discovery_result=discovery, video_aspect=video_aspect,
        target_duration=TOP_N * clip_duration, clip_duration=clip_duration,
        recent_dedupe_keys=(),
    )


def run_v2_real(pool: tuple[MaterialCandidate, ...], *, scene: str, video_aspect: str, clip_duration: float):
    """Call the Human Review V2 rank-then-secondary-dedupe sequence unchanged."""
    ranked = candidate_ranking_v2.rank_candidates_v2(
        build_scene_visual_intent(scene), pool,
        video_aspect=video_aspect, clip_duration=clip_duration,
    )
    deduped = candidate_ranking_v2.stable_secondary_dedupe([candidate for candidate, _ranking in ranked])
    by_uid = {candidate.canonical_id: ranking for candidate, ranking in ranked}
    return ranked, [(candidate, by_uid[candidate.canonical_id]) for candidate in deduped]


def _v1_decision(decision: Any, positions: Mapping[str, int]) -> dict[str, Any]:
    candidate = decision.candidate
    return {
        "asset_uid": candidate.canonical_id, "initial_position": positions[candidate.canonical_id],
        "provider": candidate.provider,
        "total_score": decision.total_score,
        "criteria": {key: getattr(decision, key) for key in (
            "orientation_score", "rank_score", "quality_score", "duration_score",
            "freshness_score", "diversity_adjustment",
        )},
        "metadata": candidate.source_info,
    }


def _v2_entry(candidate: MaterialCandidate, ranking: Any, positions: Mapping[str, int]) -> dict[str, Any]:
    return {
        "asset_uid": candidate.canonical_id, "initial_position": positions[candidate.canonical_id],
        "provider": candidate.provider,
        "total_score": ranking.total_score, "score_components": ranking.score_components,
        "reason_codes": list(ranking.reason_codes), "penalty_codes": list(ranking.penalty_codes),
        "metadata": candidate.source_info,
    }


def run_benchmark(scenarios: tuple[dict[str, Any], ...] | None = None) -> dict[str, Any]:
    """Run deterministic reports. V1 and V2 each receive the exact raw pool."""
    reports: list[dict[str, Any]] = []
    for scenario in scenarios or load_fixture():
        pool = pool_for_scenario(scenario)
        aspect, duration = str(scenario["video_aspect"]), float(scenario["clip_duration"])
        positions = {candidate.canonical_id: index + 1 for index, candidate in enumerate(pool)}
        v1 = run_v1_real(pool, video_aspect=aspect, clip_duration=duration)
        v2_ranked, v2_deduped = run_v2_real(pool, scene=str(scenario["scene"]), video_aspect=aspect, clip_duration=duration)
        v1_top = [_v1_decision(item, positions) for item in v1.decisions[:TOP_N]]
        v2_top = [_v2_entry(candidate, ranking, positions) for candidate, ranking in v2_deduped[:TOP_N]]
        if not v1_top or not v2_top:
            raise RuntimeError(f"{scenario['id']}: real ranking produced no winner")
        expected = str(scenario["expected_editorial_preference"])
        reports.append({
            "id": scenario["id"], "scene": scenario["scene"], "expected_editorial_preference": expected,
            "input_candidate_ids": [candidate.canonical_id for candidate in pool],
            "v1_input_candidate_ids": [candidate.canonical_id for candidate in pool],
            "v2_input_candidate_ids": [candidate.canonical_id for candidate in pool],
            "v1": {"winner": v1_top[0], "top3": v1_top}, "v2": {"winner": v2_top[0], "top3": v2_top},
            "same_winner": v1_top[0]["asset_uid"] == v2_top[0]["asset_uid"],
            "v1_matches_editorial_preference": v1_top[0]["asset_uid"] == expected,
            "v2_matches_editorial_preference": v2_top[0]["asset_uid"] == expected,
            "duplicate_removed_count": len(v2_ranked) - len(v2_deduped),
            "v2_negative_penalty_count": sum(len(ranking.penalty_codes) for _candidate, ranking in v2_ranked),
            "v2_narrative_reason_count": sum("emotional_match" in ranking.reason_codes for _candidate, ranking in v2_ranked),
            "top3_overlap": len({item["asset_uid"] for item in v1_top} & {item["asset_uid"] for item in v2_top}),
            "new_alternative_count": min(3, max(0, len(v2_deduped) - 1)),
        })
    overlap = sum(report["top3_overlap"] for report in reports)
    def distribution(values: list[str]) -> dict[str, int]:
        return {provider: values.count(provider) for provider in sorted(set(values))}
    return {"scenarios": reports, "metrics": {
        "winner_changed_count": sum(not report["same_winner"] for report in reports),
        "top1_same_count": sum(report["same_winner"] for report in reports),
        "top3_overlap": overlap, "top3_overlap_average": round(overlap / (TOP_N * len(reports)), 4),
        "duplicate_removed_count": sum(report["duplicate_removed_count"] for report in reports),
        "v2_negative_penalty_count": sum(report["v2_negative_penalty_count"] for report in reports),
        "v2_narrative_reason_count": sum(report["v2_narrative_reason_count"] for report in reports),
        "v1_editorial_match_count": sum(report["v1_matches_editorial_preference"] for report in reports),
        "v2_editorial_match_count": sum(report["v2_matches_editorial_preference"] for report in reports),
        "v1_winner_provider_distribution": distribution([report["v1"]["winner"]["provider"] for report in reports]),
        "v2_winner_provider_distribution": distribution([report["v2"]["winner"]["provider"] for report in reports]),
        "new_alternative_count_distribution": {
            str(count): sum(report["new_alternative_count"] == count for report in reports)
            for count in range(4)
        },
    }}


def main() -> int:
    report = run_benchmark()
    for scenario in report["scenarios"]:
        print(f"\nSCENE [{scenario['id']}]: {scenario['scene']}")
        print(f"EXPECTED_EDITORIAL_PREFERENCE: {scenario['expected_editorial_preference']}")
        print("V1:", json.dumps(scenario["v1"]["winner"], ensure_ascii=False, sort_keys=True))
        print("V1 TOP 3:", ", ".join(item["asset_uid"] for item in scenario["v1"]["top3"]))
        print("V2:", json.dumps(scenario["v2"]["winner"], ensure_ascii=False, sort_keys=True))
        print("V2 TOP 3:", ", ".join(item["asset_uid"] for item in scenario["v2"]["top3"]))
        print(f"SAME_WINNER: {'yes' if scenario['same_winner'] else 'no'}")
        print(f"EDITORIAL_MATCH: V1={'yes' if scenario['v1_matches_editorial_preference'] else 'no'} V2={'yes' if scenario['v2_matches_editorial_preference'] else 'no'}")
    print("\nMETRICS:", json.dumps(report["metrics"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
