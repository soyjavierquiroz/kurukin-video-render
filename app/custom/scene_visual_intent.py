"""Deterministic scene understanding for provider-agnostic visual ranking."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import unicodedata
from typing import Any, Mapping


_STOCK_PROVIDERS = {"pexels", "pixabay", "coverr"}


def _query_unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(" ".join(value.split()) for value in values if value and value.strip()))


def _concrete_seed_queries(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """Keep operator/generated terms as a bounded retrieval seed.

    Native stock APIs work best with short, observable English phrases.  Do
    not turn a long conceptual phrase into a request merely because it was
    supplied as a video term; the scene query below remains the contextual
    request in that case.
    """
    results: list[str] = []
    for value in values or ():
        query = " ".join(str(value or "").split())
        words = query.split()
        if (
            2 <= len(words) <= 7
            and query.isascii()
            and all(word.replace("-", "").isalpha() for word in words)
            and query not in results
        ):
            results.append(query)
    return tuple(results[:2])


def build_scene_retrieval_queries(
    intent: "SceneVisualIntent",
    provider: str,
    seed_terms: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    """Turn evidenced scene intent into 2--3 camera-observable queries.

    This deliberately does not reuse generated script terms.  Native stock
    providers receive compact English descriptions; Asset Hub receives the
    same visual meaning in Spanish for its local metadata vocabulary.
    """
    provider = str(provider or "").strip().lower()
    spanish = provider == "asset_hub"
    subject = "mujer" if spanish and "woman" in intent.literal_concepts else "hombre" if spanish and "man" in intent.literal_concepts else "persona" if spanish else "woman" if "woman" in intent.literal_concepts else "man" if "man" in intent.literal_concepts else "person"
    relationship = bool(intent.relationship_context)
    home = "home" in intent.environment or "intimate home" in intent.environment
    tense = bool(set(intent.emotional_intent) & {"anxiety", "interpersonal tension", "conflict", "self blame"}) or bool(set(intent.character_state) & {"worried", "tense", "watchful", "appeasing"})
    queries: list[str] = []
    if "observing reactions" in intent.action or "hypervigilance" in intent.emotional_intent or "watchful" in intent.character_state:
        queries.append(f"{subject} preocupada observando la reacción de otra persona" if spanish else f"worried {subject} watching another person's reaction")
    if "checking phone" in intent.action:
        queries.append(f"{subject} mirando el teléfono pensativamente en casa" if spanish else f"{subject} checking phone thoughtfully at home")
    if "thinking alone" in intent.action:
        queries.append(f"{subject} pensando a solas en casa" if spanish else f"{subject} thinking alone at home")
    if relationship and tense:
        queries.append("dos personas en conversación seria en casa" if spanish else "two people in a tense conversation at home")
    elif "seeking reassurance" in intent.action or "appeasing" in intent.character_state:
        queries.append(f"{subject} dudando al acercarse a otra persona" if spanish else f"{subject} hesitantly approaching another person")
    if tense and not relationship:
        queries.append(f"{subject} con expresión preocupada en casa" if spanish else f"worried {subject} with anxious expression at home")
    if "guilt" in intent.emotional_intent or "self blame" in intent.emotional_intent or "reflective" in intent.character_state:
        queries.append(f"{subject} reflexionando en silencio en casa" if spanish else f"{subject} reflecting quietly at home")
    if "loneliness" in intent.emotional_intent or "alone" in intent.character_state:
        queries.append(f"{subject} sola junto a una ventana en casa" if spanish else f"{subject} alone by a window at home")
    if "exhaustion" in intent.emotional_intent or "tired" in intent.character_state:
        queries.append(f"{subject} cansada haciendo una pausa en casa" if spanish else f"tired {subject} pausing quietly at home")
    if "embracing" in intent.action:
        queries.append("dos familiares abrazándose en casa" if spanish else "two family members embracing at home")
    if "arguing" in intent.action:
        queries.append("familia conversando con tensión en casa" if spanish else "family having a tense conversation at home")
    if not queries:
        action = next(iter(intent.action), "sitting")
        place = "en casa" if spanish and home else "at home" if home else "interior" if spanish else "indoors"
        queries.append(f"{subject} {action} {place}")
    queries = list(_query_unique(queries))
    # A global video-term list is a discovery seed, not a replacement for the
    # scene.  Reserve one bounded slot so it can broaden recall while the
    # first request always contains the narration-derived visual intent.
    if provider in _STOCK_PROVIDERS:
        seeds = [seed for seed in _concrete_seed_queries(seed_terms) if seed not in queries]
        if seeds:
            queries = [queries[0], seeds[0], *queries[1:]]
    return _query_unique(queries)[:3]


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(char for char in text if not unicodedata.combining(char))


def _contains(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


def _unique(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        value = str(value or "").strip().lower()
        if value and value not in result:
            result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class SceneVisualIntent:
    literal_concepts: tuple[str, ...] = ()
    emotional_intent: tuple[str, ...] = ()
    character_state: tuple[str, ...] = ()
    relationship_context: tuple[str, ...] = ()
    action: tuple[str, ...] = ()
    environment: tuple[str, ...] = ()
    cinematic_mood: tuple[str, ...] = ()
    shot_preferences: tuple[str, ...] = ()
    negative_concepts: tuple[str, ...] = ()
    visual_motif: tuple[str, ...] = ()
    temporal_context: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {key: list(value) for key, value in asdict(self).items()}


def build_scene_visual_intent(
    scene_text: str,
    *,
    editorial_profile: Mapping[str, Any] | None = None,
    visual_style: str = "",
) -> SceneVisualIntent:
    """Extract only evidenced, reusable visual signals; no model calls."""
    text = _fold(scene_text)
    words = re.findall(r"[a-zñ]+", text)
    literal = [word for word in words if len(word) >= 4 and word not in {"cuando", "porque", "tambien", "sobre", "hasta", "para"}]
    emotional: list[str] = []
    state: list[str] = []
    relation: list[str] = []
    action: list[str] = []
    environment: list[str] = []
    mood: list[str] = []
    negative: list[str] = []
    if _contains(text, ("culpa", "culpable")):
        emotional += ["guilt", "internal conflict"]; state += ["thoughtful", "anxious"]
        mood += ["intimate", "reflective"]; negative += ["smiling at camera", "commercial wellness", "influencer pose", "yoga advertisement"]
    if _contains(text, ("pensar", "preguntate", "pregúntate", "pendiente", "urgente")):
        state += ["reflective"]; action += ["thinking alone"]
    if _contains(text, ("mensaje", "telefono", "teléfono")):
        action += ["checking phone"]
    if _contains(text, ("responsabilidades", "responsable", "irresponsable", "ser util", "ser útil")):
        emotional += ["self blame", "internal conflict"]; state += ["worried", "reflective"]
        mood += ["intimate", "reflective"]
        negative += ["commercial", "influencer pose", "corporate", "advertising pose"]
    if _contains(text, ("agot", "cansad", "fatiga")):
        emotional += ["exhaustion"]; state += ["tired"]; mood += ["quiet", "reflective"]
        negative += ["energetic pose", "commercial smile"]
    if _contains(text, ("ansiedad", "ansios", "miedo", "preocup")):
        emotional += ["anxiety"]; state += ["worried", "tense"]; mood += ["intimate", "tense"]
        negative += ["celebration", "smiling at camera", "gaming", "influencer pose", "corporate", "unrelated romance", "advertising pose"]
    if _contains(text, ("tono de voz", "cambia el tono", "mas serio", "más serio", "responde diferente", "mal humor", "molesto", "molesta", "silencios", "gestos", "cambios de humor", "cambio de humor", "cambie de humor", "estado de animo", "estado de ánimo", "cambio de animo", "cambio de ánimo", "humor de otra persona")):
        emotional += ["interpersonal tension", "hypervigilance", "concern"]
        state += ["watchful", "worried", "contained"]
        relation += ["close relationship"]
        action += ["observing reactions"]
        mood += ["tense", "intimate", "restrained"]
        negative += ["gaming", "influencer pose", "corporate", "celebration", "party", "unrelated romance", "advertising pose", "commercial"]
    if _contains(text, ("arreglarlo", "arreglar", "disculp", "que hice", "qué hice", "responsable de emociones")):
        emotional += ["self blame", "interpersonal tension"]
        state += ["worried", "appeasing"]
        action += ["seeking reassurance"]
        mood += ["intimate", "tense"]
        negative += ["gaming", "influencer pose", "corporate", "celebration", "party", "unrelated romance", "advertising pose", "commercial"]
    if _contains(text, ("abandono", "abandonad", "nadie")):
        emotional += ["abandonment", "loneliness"]; state += ["vulnerable", "alone"]; mood += ["melancholic"]
        negative += ["party", "celebration", "commercial", "smiling at camera"]
    if _contains(text, ("soledad", "sola", "solo")):
        emotional += ["loneliness"]; state += ["alone", "reflective"]; mood += ["quiet", "intimate"]
        negative += ["party", "celebration", "commercial", "influencer pose"]
    if _contains(text, ("discute", "conflicto", "pelea")):
        emotional += ["conflict"]; relation += ["family conflict"]; action += ["arguing"]; mood += ["tense"]
    if _contains(text, ("familia", "madre", "padre", "hija", "hijo", "pareja")):
        relation += ["family" if "familia" in text else "close relationship"]
    if _contains(text, ("reconcili", "perdon", "abraza")):
        emotional += ["reconciliation"]; relation += ["repairing relationship"]; action += ["embracing"]; mood += ["warm", "intimate"]
        negative += ["corporate", "handshake", "advertising pose"]
    if _contains(text, ("descans", "sentada", "sentado", "sofa", "sillon")):
        action += ["resting" if "descans" in text else "sitting"]
    if _contains(text, ("casa", "hogar", "sofa", "sillon", "cocina", "habitacion")):
        environment += ["home"]
    if not environment and _contains(text, ("descans", "soledad", "culpa")):
        environment += ["intimate home"]
    gender = str((editorial_profile or {}).get("subject_gender") or "").lower()
    if gender == "feminine": literal.insert(0, "woman")
    elif gender == "masculine": literal.insert(0, "man")
    if "mujer" in text: literal.insert(0, "woman")
    if "hombre" in text: literal.insert(0, "man")
    if visual_style and str(visual_style).strip().lower() not in {"none", "default"}:
        mood.append(str(visual_style).strip().lower())
    return SceneVisualIntent(_unique(literal)[:12], _unique(emotional), _unique(state), _unique(relation), _unique(action), _unique(environment), _unique(mood), ("clear human subject",), _unique(negative))
