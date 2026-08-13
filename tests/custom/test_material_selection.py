import unittest
from types import SimpleNamespace

from app.custom.material_discovery import MaterialCandidate, MaterialDiscoveryResult
from app.custom.material_selection import orientation_score, select_material_candidates


def candidate(key, term="one", provider="pexels", **kwargs):
    return MaterialCandidate(provider, key, key, term, **kwargs)


def discovery(*items):
    return SimpleNamespace(candidates=items)


class TestMaterialSelection(unittest.TestCase):
    def test_orientation_priorities(self):
        vertical = candidate("v", orientation="vertical-9x16")
        square = candidate("s", orientation="square")
        horizontal = candidate("h", orientation="horizontal-16x9")
        self.assertGreater(orientation_score(vertical, "9:16"), orientation_score(square, "9:16"))
        self.assertGreater(orientation_score(horizontal, "16:9"), orientation_score(vertical, "16:9"))

    def test_term_coverage_rank_quality_duration_and_stability(self):
        items = (candidate("low", "one", rank=8, width=640, height=360, duration=2),
                 candidate("high", "one", rank=1, width=1920, height=1080, duration=5),
                 candidate("two", "two", rank=30))
        result = select_material_candidates(discovery_result=discovery(*items), video_aspect="16:9", target_duration=15, clip_duration=5)
        self.assertEqual(result.covered_terms, ("one", "two"))
        self.assertEqual(result.decisions[0].candidate.dedupe_key, "high")
        self.assertEqual(result.selected_count, 3)

    def test_diversity_recent_fallback_no_duplicates_and_shortfall(self):
        items = (candidate("p1", provider="pexels", rank=1), candidate("p2", provider="pexels", rank=1),
                 candidate("x", provider="pixabay", rank=1), candidate("p1", provider="pexels", rank=2))
        result = select_material_candidates(discovery_result=discovery(*items), video_aspect="16:9", target_duration=20, clip_duration=5, recent_dedupe_keys=("p1", "p2"))
        self.assertEqual(result.decisions[0].candidate.provider, "pixabay")
        self.assertEqual(len({d.candidate.dedupe_key for d in result.decisions}), result.selected_count)
        self.assertTrue(result.used_recent_fallback)
        self.assertEqual(result.shortfall, 1)

    def test_asset_hub_uids_are_not_collapsed(self):
        result = select_material_candidates(discovery_result=discovery(candidate("kurukin_media:a", provider="asset_hub"), candidate("kurukin_media:b", provider="asset_hub")), video_aspect="9:16", target_duration=10, clip_duration=5)
        self.assertEqual([d.candidate.dedupe_key for d in result.decisions], ["kurukin_media:a", "kurukin_media:b"])


if __name__ == "__main__": unittest.main()
