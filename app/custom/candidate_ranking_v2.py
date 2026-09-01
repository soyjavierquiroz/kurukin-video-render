"""Explainable, deterministic provider-agnostic review ranking."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
import unicodedata
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from app.custom.material_selection import _duration_score, orientation_score
from app.custom.scene_visual_intent import SceneVisualIntent

WEIGHTS = {"semantic_relevance": .30, "narrative_emotional_fit": .25, "cinematic_editorial": .15, "technical_usability": .15, "sequence_adjustment": .07, "subtitle_overlay_safety": .05, "provenance_confidence": .03}
_UNKNOWN_TEXT = {"", "none", "null", "unknown", "nan", "n/a", "na", "undefined"}
_DENIED_RIGHTS = {"denied", "unauthorized", "not_authorized", "not authorized", "not_production_eligible", "not production eligible"}
_NEGATIVE_ALIASES = {
    "celebration": (("celebration",), ("celebrating",), ("birthday",), ("party",), ("celebracion",), ("cumpleanos",), ("fiesta",), ("festejo",)),
    "smiling": (("smile",), ("smiling",), ("happy", "portrait"), ("sonrisa",), ("sonriendo",), ("sonriente",)),
    "commercial": (("commercial",), ("advertising",), ("promotional",), ("publicidad",), ("comercial",), ("promocional",), ("influencer",)),
    "gaming": (("gaming",), ("gamer",), ("videogame",), ("streamer",)),
    "corporate": (("corporate",), ("office", "portrait"), ("business", "portrait")),
}

def _fold(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value.lower()).strip()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _usable_text(value: Any) -> str:
    text = _fold(value)
    return "" if text in _UNKNOWN_TEXT else text


def _tokens(value: Any) -> set[str]: return set(re.findall(r"[a-z0-9]+", _usable_text(value)))
def _metadata(candidate: Any) -> Mapping[str, Any]:
    if isinstance(candidate, Mapping):
        value = candidate.get("metadata", candidate.get("source_info", {}))
        return value if isinstance(value, Mapping) else {}
    value = getattr(candidate, "source_info", {})
    return value if isinstance(value, Mapping) else {}


def _candidate_value(candidate: Any, key: str) -> Any:
    return candidate.get(key) if isinstance(candidate, Mapping) else getattr(candidate, key, None)


def _identity_text(value: Any, *, folded: bool = False) -> str:
    """Return a present identity value without turning missing values into keys."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or text.casefold() in _UNKNOWN_TEXT:
        return ""
    return _fold(text) if folded else text


def normalize_source_url(value: Any) -> str:
    """Safely normalize a source URL without changing its path or query."""
    raw = _identity_text(value)
    if not raw:
        return ""
    without_fragment = raw.split("#", 1)[0]
    try:
        parsed = urlsplit(without_fragment)
    except (TypeError, ValueError):
        return without_fragment
    if not parsed.scheme or not parsed.netloc:
        return without_fragment
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return without_fragment
    if not hostname:
        return without_fragment
    userinfo = ""
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@"
    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{userinfo}{host}{f':{port}' if port is not None else ''}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def candidate_identity_keys(candidate: Any) -> tuple[str, ...]:
    """Return available primary and secondary review-dedupe identities.

    ``canonical_id`` is the current materializable Asset Hub ``asset_uid``;
    ``dedupe_key`` remains a separate primary representation.  Asset Hub
    preserves its secondary source fields in ``source_info`` (or serialized
    plan ``metadata``), where ``source_identity`` is its per-source identity.
    """
    metadata = _metadata(candidate)
    keys: list[str] = []

    for field in ("asset_uid", "canonical_id", "dedupe_key"):
        if value := _identity_text(_candidate_value(candidate, field)):
            keys.append(f"primary:{value}")

    provider = _identity_text(_candidate_value(candidate, "provider"), folded=True)
    provider_asset_id = _identity_text(metadata.get("provider_asset_id"))
    if provider and provider_asset_id:
        keys.append(f"provider_asset:{provider}:{provider_asset_id}")

    if source_identity := _identity_text(metadata.get("source_identity"), folded=True):
        keys.append(f"source_identity:{source_identity}")

    if source_url := normalize_source_url(metadata.get("source_url")):
        keys.append(f"source_url:{source_url}")

    return tuple(dict.fromkeys(keys))


def stable_secondary_dedupe(candidates: Iterable[Any]) -> list[Any]:
    """Keep the first candidate for every available primary/secondary key."""
    unique: list[Any] = []
    seen: set[str] = set()
    for candidate in candidates:
        keys = candidate_identity_keys(candidate)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        unique.append(candidate)
    return unique
def _text(candidate: Any) -> str:
    data = _metadata(candidate)
    values: list[Any] = []
    # Providers expose different subsets of this contract. Absence is kept
    # unavailable rather than converted into a negative score.
    for key in ("title", "description", "keywords", "filename", "tags", "visual_description", "action_description", "search_text", "embedding_text", "primary_theme", "primary_topic", "presentation", "visual_presentation"):
        value = data.get(key)
        values.extend(value) if isinstance(value, (list, tuple, set)) else values.append(value)
    for key in ("title", "description", "filename", "search_term"):
        values.append(_candidate_value(candidate, key))
    return " ".join(text for value in values if (text := _usable_text(value)))


def _match(phrases: Iterable[str], text: str) -> float | None:
    wanted = {_usable_text(item) for item in phrases if _usable_text(item)}
    hay = _usable_text(text)
    if not wanted or not hay:
        return None
    hay_tokens = _tokens(hay)
    matched = sum(1 for item in wanted if item in hay or bool(_tokens(item) & hay_tokens))
    return matched / len(wanted)


def _has_phrase(tokens: set[str], phrase: tuple[str, ...]) -> bool:
    return bool(phrase) and set(phrase) <= tokens


def _negative_categories(intent: SceneVisualIntent, text: str) -> set[str]:
    """Return only explicit, normalized metadata evidence (never guesses)."""
    wanted: set[str] = set()
    negatives = " ".join(intent.negative_concepts)
    if any(word in negatives for word in ("celebration", "party")): wanted.add("celebration")
    if any(word in negatives for word in ("smiling", "smile")): wanted.add("smiling")
    if any(word in negatives for word in ("commercial", "advertis", "influencer")): wanted.add("commercial")
    if "gaming" in negatives: wanted.add("gaming")
    if "corporate" in negatives: wanted.add("corporate")
    tokens = _tokens(text)
    return {category for category in wanted if any(_has_phrase(tokens, phrase) for phrase in _NEGATIVE_ALIASES[category])}


def has_strong_scene_intent(intent: SceneVisualIntent) -> bool:
    return bool(intent.emotional_intent or intent.relationship_context or intent.action)
def _clamp(value: float) -> float: return round(max(0.0, min(1.0, value)), 4)


def _positive_number(value: Any) -> float | None:
    try: number = float(value)
    except (TypeError, ValueError): return None
    return number if isfinite(number) and number > 0 else None


def _normalised_number(value: Any) -> float | None:
    number = _positive_number(value)
    return min(1.0, number if number <= 1 else number / 100) if number is not None else None


def _identity(candidate: Any) -> str:
    return _usable_text(_candidate_value(candidate, "canonical_id")) or _usable_text(_candidate_value(candidate, "dedupe_key"))


@dataclass(frozen=True)
class CandidateEligibility:
    eligible: bool
    rejection_codes: tuple[str, ...] = ()


def evaluate_candidate_eligibility(candidate: Any) -> CandidateEligibility:
    """Evaluate the common, provider-neutral review candidate contract."""
    codes: list[str] = []
    if not _usable_text(_candidate_value(candidate, "provider")): codes.append("missing_provider")
    if not _identity(candidate): codes.append("missing_canonical_identity")
    info = _metadata(candidate)
    media_type = _usable_text(info.get("media_type"))
    if media_type and media_type != "video": codes.append("non_video_media")
    # Asset Hub preserves rights_state/provenance_state but no local enum;
    # reject only facts that unambiguously mean authorization was denied.
    for key in ("rights_state", "rights", "production_eligible", "authorized"):
        value = info.get(key)
        explicit_false = key in {"production_eligible", "authorized"} and _usable_text(value) in {"false", "no"}
        if value is False or explicit_false or _usable_text(value) in _DENIED_RIGHTS:
            codes.append("explicitly_not_production_eligible")
            break
    return CandidateEligibility(not codes, tuple(codes))


def _orientation_component(candidate: Any, video_aspect: str) -> float | None:
    width, height = _positive_number(_candidate_value(candidate, "width")), _positive_number(_candidate_value(candidate, "height"))
    orientation = _usable_text(_candidate_value(candidate, "orientation"))
    if (width is None or height is None) and not any(token in orientation for token in ("portrait", "vertical", "landscape", "horizontal", "square", "9:16", "4:5", "16:9")): return None
    return min(1.0, orientation_score(candidate, video_aspect) / 44)


def _technical_component(candidate: Any, video_aspect: str, clip_duration: float) -> float | None:
    signals: list[tuple[float, float]] = []
    if (orientation := _orientation_component(candidate, video_aspect)) is not None: signals.append((.45, orientation))
    if _positive_number(_candidate_value(candidate, "duration")) is not None: signals.append((.30, min(1.0, _duration_score(candidate, clip_duration) / 20)))
    width, height = _positive_number(_candidate_value(candidate, "width")), _positive_number(_candidate_value(candidate, "height"))
    if width is not None and height is not None: signals.append((.25, min(1.0, (width * height) / 2073600)))
    return sum(weight * score for weight, score in signals) / sum(weight for weight, _score in signals) if signals else None


def _cinematic_component(info: Mapping[str, Any]) -> float | None:
    signals = [value for key in ("editorial_quality", "quality_score", "vertical_suitability", "horizontal_suitability", "suitability") if (value := _normalised_number(info.get(key))) is not None]
    if info.get("contains_people") is True: signals.append(.7)
    visibility = _usable_text(info.get("person_visibility"))
    if visibility in {"clear", "visible"}: signals.append(1.0)
    elif visibility in {"partial", "partially_visible"}: signals.append(.5)
    if _usable_text(info.get("visual_presentation") or info.get("presentation")) in {"natural", "editorial", "cinematic", "documentary"}: signals.append(.8)
    if _usable_text(info.get("camera_motion")) in {"static", "handheld", "tracking", "pan", "tilt", "dolly"}: signals.append(.7)
    return sum(signals) / len(signals) if signals else None


def _overlay_component(info: Mapping[str, Any]) -> float | None:
    for key in ("safe_for_subtitles", "safe_for_text_overlay"):
        if info.get(key) is True: return 1.0
        if info.get(key) is False: return 0.0
    return None


def _provenance_component(info: Mapping[str, Any]) -> float | None:
    if (value := _normalised_number(info.get("provenance_state"))) is not None: return value
    if info.get("provenance_state") is True: return 1.0
    if info.get("provenance_state") is False: return 0.0
    return 1.0 if _usable_text(info.get("source_identity")) else None

@dataclass(frozen=True)
class CandidateRanking:
    total_score: float
    score_components: dict[str, float]
    reason_codes: tuple[str, ...]
    penalty_codes: tuple[str, ...]

def rank_candidate(intent: SceneVisualIntent, candidate: Any, *, video_aspect: str, clip_duration: float, previous_candidates: Iterable[Any] = ()) -> CandidateRanking:
    del previous_candidates  # Sequence-aware ranking is intentionally deferred.
    text = _text(candidate); info = _metadata(candidate); reasons: list[str] = []; penalties: list[str] = []
    semantic = _match((*intent.literal_concepts, *intent.action, *intent.environment, *intent.relationship_context), text)
    emotional = _match((*intent.emotional_intent, *intent.character_state, *intent.cinematic_mood), text)
    if semantic is not None and semantic >= .34: reasons.append("semantic_match")
    if emotional is not None and emotional >= .34: reasons.append("emotional_match")
    action_match = _match(intent.action, text)
    environment_match = _match(intent.environment, text)
    if action_match is not None and action_match >= .5: reasons.append("relevant_action")
    if environment_match is not None and environment_match >= .5: reasons.append("domestic_context")
    negative_hits = sorted(_negative_categories(intent, text))
    if negative_hits:
        if emotional is not None: emotional *= .25
        if semantic is not None: semantic *= .75
        penalties.extend("negative_" + item for item in negative_hits)
        # Keep the serialized code consumed by existing review UI/tests while
        # retaining the normalized category above.
        if "commercial" in negative_hits:
            penalties.append("negative_commercial_aesthetic")
    raw_components = {
        "semantic_relevance": semantic,
        "narrative_emotional_fit": emotional,
        "cinematic_editorial": _cinematic_component(info),
        "technical_usability": _technical_component(candidate, video_aspect, clip_duration),
        # Sequence is unavailable until sequence-aware preprocessing exists.
        "subtitle_overlay_safety": _overlay_component(info),
        "provenance_confidence": _provenance_component(info),
    }
    components = {key: _clamp(value) for key, value in raw_components.items() if value is not None}
    if info.get("contains_people") is True: reasons.append("human_subject")
    total = (sum(WEIGHTS[key] * value for key, value in components.items()) / sum(WEIGHTS[key] for key in components)) if components else 0.0
    if negative_hits and has_strong_scene_intent(intent):
        # Explicit contrary metadata is editorially disqualifying.  Unknown
        # metadata never reaches this branch and remains eligible.
        total = 0.0
        penalties.append("explicit_narrative_contradiction")
    elif negative_hits:
        total -= .15
    return CandidateRanking(_clamp(total), components, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(penalties)))

def rank_candidates_v2(intent: SceneVisualIntent, candidates: Iterable[Any], *, video_aspect: str, clip_duration: float, previous_candidates: Iterable[Any] = ()) -> list[tuple[Any, CandidateRanking]]:
    indexed = [(index, candidate, rank_candidate(intent, candidate, video_aspect=video_aspect, clip_duration=clip_duration, previous_candidates=previous_candidates)) for index, candidate in enumerate(candidates) if evaluate_candidate_eligibility(candidate).eligible]
    # Preserve UNKNOWN as eligible, but never let a technically rich unknown
    # (including metadata-empty Asset Hub records) leapfrog positive scene
    # evidence.  Discovery order remains the tie-break within the unknown
    # bucket, which keeps genuine scarcity possible without fabricating a
    # match.
    def sort_key(item: tuple[int, Any, CandidateRanking]) -> tuple[int, int, float, int]:
        ranking = item[2]
        editorial_evidence = max(
            ranking.score_components.get("semantic_relevance", 0.0),
            ranking.score_components.get("narrative_emotional_fit", 0.0),
        )
        contradiction = "explicit_narrative_contradiction" in ranking.penalty_codes
        # A rich but narratively-empty candidate cannot overtake a candidate
        # with actual scene evidence merely through technical/cinematic data.
        # This intentionally includes an empty metadata record: it is UNKNOWN,
        # not a contradiction, but it is still pure zero editorial evidence.
        pure_unknown = has_strong_scene_intent(intent) and editorial_evidence == 0
        return (1 if contradiction else 0, 1 if pure_unknown else 0, -ranking.total_score if editorial_evidence > 0 else 0.0, item[0])
    indexed.sort(key=sort_key)
    return [(candidate, ranking) for _index, candidate, ranking in indexed]
