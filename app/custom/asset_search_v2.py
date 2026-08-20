"""Lexical visual query helpers for Kurukin Asset Hub search V2."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Sequence


_STOPWORDS = {
    "a", "al", "algo", "ante", "asi", "aunque", "cada", "como", "con",
    "contra", "cuando", "de", "del", "desde", "donde", "e", "el", "ella",
    "ellas", "ellos", "en", "entre", "era", "eran", "eres", "es", "esa",
    "ese", "eso", "esta", "estaba", "estaban", "estas", "este", "esto",
    "fue", "ha", "habia", "hacia", "hasta", "iba", "la", "las", "le",
    "les", "lo", "los", "mas", "me", "mi", "mis", "ni", "no", "nos",
    "o", "para", "pero", "porque", "por", "que", "se", "ser", "si",
    "sin", "su", "sus", "te", "tenia", "ti", "tu", "tus", "un", "una",
    "uno", "y", "ya",
}
_NARRATIVE_NOISE = {
    "ademas", "aprendiste", "aquello", "buscando", "creces", "crecés",
    "demas", "demás", "despues", "después", "elegis", "elegís",
    "entonces", "llegaron", "necesitaba", "necesitabas", "necesitado",
    "necesitan", "necesito", "necesitó", "ocurrio", "ocurrió", "pasado",
    "podias", "podías", "puede", "pueden", "quizas", "quizás", "seguia",
    "seguís", "sentias", "sentías", "siempre", "tambien", "también",
    "todavia", "todavía",
}

_SUBJECTS = {
    "adulto", "adulta", "amiga", "amigo", "familia", "hija", "hijo",
    "hombre", "joven", "madre", "mama", "mamá", "mujer", "nina", "niña",
    "nino", "niño", "padre", "pareja", "persona", "personas",
}
_ACTIONS = {
    "abrazando", "abrazandose", "abrazar", "acompañar", "acompanar",
    "ayudar", "calma", "cuidar", "discute", "discutiendo", "escucha",
    "escuchara", "llorando", "llorar", "mira", "mirando", "observa",
    "pensativa", "pensativo", "proteger", "protegiera", "rescatar",
    "rescatadas", "rescatados", "salvar", "sostiene", "sosteniendo",
}
_MOODS = {
    "ansiedad", "asustada", "asustado", "calma", "culpa", "culpable",
    "dolor", "emocional", "fuerte", "introspeccion", "introspección",
    "miedo", "nostalgia", "preocupacion", "preocupación", "preocupada",
    "preocupado", "resiliente", "soledad", "sola", "solo", "triste",
    "tristeza", "vulnerabilidad", "vulnerable",
}
_OBJECTS = {
    "carta", "celular", "coche", "computadora", "documento", "espejo",
    "foto", "libro", "laptop", "mesa", "telefono", "teléfono", "ventana",
}
_SETTINGS = {
    "casa", "ciudad", "cocina", "escuela", "habitacion", "habitación",
    "hospital", "oficina", "parque", "playa", "sala", "trabajo",
}

_CONCEPT_RULES: tuple[tuple[set[str], tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        {"escucha", "escuchara", "proteger", "protegiera", "apoyo", "necesito"},
        ("vulnerable", "apoyo emocional"),
        ("consuelo", "acompañamiento"),
        ("protección emocional", "vulnerabilidad"),
    ),
    (
        {"sostiene", "sosteniendo", "cargar", "cuida", "cuidar"},
        ("preocupada", "miedo"),
        ("cuidadora", "apoyo emocional"),
        ("responsabilidad emocional", "ansiedad"),
    ),
    (
        {"rescatar", "rescatarte", "rescatadas", "rescatados", "salvar"},
        ("dependencia emocional", "relación"),
        ("cuidadora", "apoyo emocional"),
        ("relación desequilibrada", "rescate"),
    ),
    (
        {"discute", "discutiendo"},
        ("tensión emocional", "preocupación"),
        ("conflicto", "relación"),
        ("discusión familiar", "desacuerdo"),
    ),
    (
        {"miedo", "asustada", "asustado"},
        ("miedo", "preocupación"),
        ("persona", "ansiedad"),
        ("vulnerabilidad emocional",),
    ),
    (
        {"soledad", "sola", "solo", "nadie"},
        ("persona", "soledad"),
        ("aislamiento", "introspección"),
        ("vulnerabilidad emocional",),
    ),
)

_TERM_ALIASES = {
    "asustada": ("miedo", "ansiedad", "preocupación"),
    "asustado": ("miedo", "ansiedad", "preocupación"),
    "escuchara": ("apoyo emocional", "consuelo"),
    "protegiera": ("protección emocional", "vulnerable"),
    "rescatarte": ("dependencia emocional", "rescate"),
    "rescatadas": ("dependencia emocional", "rescate"),
    "rescatados": ("dependencia emocional", "rescate"),
    "sostiene": ("cuidadora", "responsabilidad emocional"),
    "fuerte": ("resiliente",),
    "triste": ("tristeza", "vulnerable"),
    "tristeza": ("vulnerable", "introspección"),
}


def _fold(text: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _words(text: Any) -> list[str]:
    return re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ]+", str(text or ""), flags=re.UNICODE)


def _clean_tokens(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    forbidden = {_fold(item) for item in (*_STOPWORDS, *_NARRATIVE_NOISE)}
    for value in values:
        for word in _words(value):
            folded = _fold(word)
            if len(folded) < 3 or folded in forbidden:
                continue
            if folded not in seen:
                result.append(word.lower())
                seen.add(folded)
    return result


def _pick(tokens: Sequence[str], allowed: set[str]) -> list[str]:
    allowed_folded = {_fold(item) for item in allowed}
    return [token for token in tokens if _fold(token) in allowed_folded]


def _append_unique(target: list[str], values: Sequence[Any], *, limit: int = 7) -> None:
    seen = {_fold(item) for item in target}
    for value in values:
        text = str(value or "").strip().lower()
        folded = _fold(text)
        if folded and folded not in seen:
            target.append(text)
            seen.add(folded)
        if len(target) >= limit:
            return


def _query(values: Sequence[Any]) -> str:
    parts: list[str] = []
    _append_unique(parts, values, limit=7)
    return " ".join(parts).strip()


def _token_set(query: str) -> frozenset[str]:
    return frozenset(_fold(word) for word in _words(query))


def _is_distinct(query: str, existing: Sequence[str]) -> bool:
    current = _token_set(query)
    if not current:
        return False
    for other in existing:
        previous = _token_set(other)
        if current == previous:
            return False
        if len(current) >= 3 and len(previous) >= 3:
            overlap = len(current & previous)
            if overlap / max(len(current), 1) >= 0.85 and overlap / max(len(previous), 1) >= 0.85:
                return False
    return True


def _dedupe_phrases(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        folded = _fold(value)
        if not folded:
            continue
        if any(folded in _fold(other) or _fold(other) in folded for other in result):
            continue
        result.append(value)
    return result


def _concept_mood(value: str) -> bool:
    folded = _fold(value)
    return (
        any(word in folded for word in ("miedo", "ansiedad", "vulner", "preocup", "soledad", "triste", "dependencia"))
        or folded == "relacion"
    )


def _concept_relation(value: str) -> bool:
    folded = _fold(value)
    return any(word in folded for word in ("apoyo", "consuelo", "acompan", "cuidadora")) and "proteccion" not in folded


def _concept_theme(value: str) -> bool:
    folded = _fold(value)
    return any(word in folded for word in ("proteccion", "responsabilidad", "desequilibrada", "rescate", "discusion", "desacuerdo"))


def _signals(tokens: Sequence[str]) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str]]:
    folded = {_fold(token) for token in tokens}
    moods = _pick(tokens, _MOODS)
    actions = _pick(tokens, _ACTIONS)
    subjects = _pick(tokens, _SUBJECTS)
    objects = _pick(tokens, _OBJECTS)
    settings = _pick(tokens, _SETTINGS)
    concepts: list[str] = []
    if "nadie" in folded:
        _append_unique(
            concepts,
            ("persona", "soledad", "aislamiento", "introspección", "vulnerabilidad emocional"),
            limit=12,
        )
    for trigger, mood_terms, relation_terms, theme_terms in _CONCEPT_RULES:
        if folded & {_fold(item) for item in trigger}:
            _append_unique(concepts, (*mood_terms, *relation_terms, *theme_terms), limit=12)
    for token in tokens:
        _append_unique(concepts, _TERM_ALIASES.get(_fold(token), ()), limit=12)
    return subjects, actions, moods, objects, settings, concepts


def build_visual_queries_v2(
    scene_text: str,
    existing_terms: Sequence[str] | None = None,
    *,
    max_queries: int = 3,
) -> tuple[str, ...]:
    """Build compact, diverse lexical visual queries without extra AI calls."""
    hint_tokens = _clean_tokens(existing_terms or ())
    scene_tokens = _clean_tokens([scene_text])
    tokens = list(dict.fromkeys([*hint_tokens, *scene_tokens]))
    if not tokens:
        return ()

    subjects, actions, moods, objects, settings, concepts = _signals(tokens)
    subject = subjects[:1] or (["persona"] if (actions or moods or concepts) else [])
    relation_subject = subjects[:1] or (["persona"] if (actions or objects or settings) else [])

    concept_moods = _dedupe_phrases([item for item in concepts if _concept_mood(item)])
    concept_relations = _dedupe_phrases([item for item in concepts if _concept_relation(item)])
    concept_themes = _dedupe_phrases([item for item in concepts if _concept_theme(item)])

    raw_moods = list(moods)
    if raw_moods:
        _append_unique(raw_moods, concept_moods, limit=2)
    else:
        _append_unique(raw_moods, concept_moods[:1], limit=2)
        if len(raw_moods) < 2:
            _append_unique(raw_moods, concept_relations[:1], limit=2)
        if len(raw_moods) < 2:
            _append_unique(raw_moods, concept_moods[1:2], limit=2)

    theme_lane = concept_themes[:2] or concept_relations[:2] or concept_moods[:2]
    if len(_query(theme_lane).split()) < 3:
        _append_unique(theme_lane, concept_moods[:1], limit=3)

    lanes = (
        [*subject, *raw_moods[:2]],
        [*relation_subject, *(concept_relations[:2] or actions[:2]), *objects[:1], *settings[:1]],
        theme_lane,
    )

    queries: list[str] = []
    for lane in lanes:
        query = _query(lane)
        word_count = len(query.split())
        if 3 <= word_count <= 7 and _is_distinct(query, queries):
            queries.append(query)
        if len(queries) >= max(1, min(max_queries, 3)):
            break

    if not queries and (moods or actions or concepts):
        fallback = _query([*subject, *(moods[:2] or concepts[:2]), *actions[:1]])
        if 3 <= len(fallback.split()) <= 7:
            queries.append(fallback)

    return tuple(queries[: max(1, min(max_queries, 3))])
