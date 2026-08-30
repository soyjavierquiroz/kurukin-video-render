"""Local-only topic script planning helpers for Kurukin intents."""

from __future__ import annotations

import re
from typing import Any


DEFAULT_TOPIC_PRESET = "educational"
SUPPORTED_TOPIC_PRESETS = {
    "educational",
    "listicle",
    "problem_solution",
    "sales",
    "story",
}
TOPIC_PLAN_STATUS_NEEDS_AUDIO = "NEEDS_AUDIO"
TOPIC_PLAN_REASON_NEEDS_AUDIO = "needs_audio_or_tts"

_STOPWORDS = {
    "al",
    "and",
    "con",
    "de",
    "del",
    "el",
    "en",
    "for",
    "la",
    "las",
    "los",
    "para",
    "por",
    "que",
    "the",
    "una",
    "un",
    "used",
    "y",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_preset(value: Any) -> str:
    preset = _clean_text(value).lower()
    return preset if preset in SUPPORTED_TOPIC_PRESETS else DEFAULT_TOPIC_PRESET


def _duration_seconds(value: Any) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = 45
    return max(4, min(duration, 300))


def _language(value: Any) -> str:
    return _clean_text(value).lower() or "es"


def _is_english(language: str) -> bool:
    return language.startswith("en")


def generate_local_script(
    topic: str,
    preset: str,
    duration_seconds: int,
    language: str,
) -> str:
    """Generate a small deterministic script from local templates."""

    clean_topic = _clean_text(topic) or "este tema"
    clean_preset = _clean_preset(preset)
    duration = _duration_seconds(duration_seconds)
    lang = _language(language)

    if _is_english(lang):
        templates = {
            "educational": [
                f"Hook: {clean_topic} can get expensive if you miss the basics.",
                f"Point 1: Start with the visible facts around {clean_topic}.",
                "Point 2: Compare options before deciding.",
                "Point 3: Keep a short checklist and confirm the risky details.",
                "Close: Save this and review it before taking the next step.",
            ],
            "listicle": [
                f"Here are the key points about {clean_topic}.",
                "Number 1: Look for the first obvious warning sign.",
                "Number 2: Check what most people skip.",
                "Number 3: Decide only after comparing the tradeoffs.",
                "Close: Use this list as a quick filter.",
            ],
            "problem_solution": [
                f"Problem: {clean_topic} often feels simple until the hidden details appear.",
                "Why it matters: one missed detail can change the whole result.",
                "Solution: slow down, compare evidence, and ask for proof.",
                "Action: choose the option with fewer unknowns.",
            ],
            "sales": [
                f"If {clean_topic} matters to you, clarity is the first win.",
                "Show the pain: confusion wastes time and money.",
                "Show the value: a simple plan makes the decision easier.",
                "Close: take the next step with the checklist ready.",
            ],
            "story": [
                f"Someone starts with {clean_topic} and thinks it will be easy.",
                "Then a small detail changes the decision.",
                "They pause, compare the facts, and avoid the expensive mistake.",
                "Close: the lesson is simple: verify before you commit.",
            ],
        }
    else:
        templates = {
            "educational": [
                f"Hook: {clean_topic} puede salir caro si pasas por alto lo basico.",
                f"Punto 1: empieza revisando los datos visibles sobre {clean_topic}.",
                "Punto 2: compara opciones antes de decidir.",
                "Punto 3: usa una lista corta y confirma los detalles de riesgo.",
                "Cierre: guarda esta guia y revisala antes del siguiente paso.",
            ],
            "listicle": [
                f"Estos son los puntos clave sobre {clean_topic}.",
                "Numero 1: detecta la primera senal de alerta.",
                "Numero 2: revisa lo que casi todos omiten.",
                "Numero 3: decide despues de comparar los riesgos.",
                "Cierre: usa esta lista como filtro rapido.",
            ],
            "problem_solution": [
                f"Problema: {clean_topic} parece simple hasta que aparecen detalles ocultos.",
                "Por que importa: un detalle omitido puede cambiar todo el resultado.",
                "Solucion: baja la velocidad, compara evidencia y pide pruebas.",
                "Accion: elige la opcion con menos dudas abiertas.",
            ],
            "sales": [
                f"Si {clean_topic} te importa, la claridad es la primera ganancia.",
                "Dolor: la confusion consume tiempo y dinero.",
                "Valor: un plan simple hace mas facil decidir.",
                "Cierre: da el siguiente paso con la lista preparada.",
            ],
            "story": [
                f"Alguien empieza con {clean_topic} pensando que sera facil.",
                "Luego un detalle pequeno cambia la decision.",
                "Se detiene, compara los datos y evita un error costoso.",
                "Cierre: la leccion es simple: verifica antes de comprometerte.",
            ],
        }

    lines = templates[clean_preset]
    if duration <= 20:
        lines = lines[:3]
    return "\n".join(lines)


def _script_units(script: str) -> list[str]:
    units = []
    for line in _clean_text(script).splitlines():
        clean = re.sub(r"^\s*[-*]?\s*(?:\d+[.)]\s*)?", "", line).strip()
        if clean:
            units.append(clean)
    if units:
        return units

    sentences = re.split(r"(?<=[.!?])\s+", _clean_text(script))
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def split_script_into_scenes(script: str, duration_seconds: int) -> list[dict[str, Any]]:
    """Split script lines into simple timed scenes."""

    units = _script_units(script)
    if not units:
        return []

    total_duration = _duration_seconds(duration_seconds)
    base_duration = max(1, total_duration // len(units))
    remainder = max(0, total_duration - (base_duration * len(units)))

    scenes: list[dict[str, Any]] = []
    for index, text in enumerate(units, start=1):
        duration = base_duration + (1 if index <= remainder else 0)
        scenes.append(
            {
                "index": index,
                "duration_seconds": duration,
                "text": text,
                "visual_keywords": [],
            }
        )
    return scenes


def _keyword_tokens(value: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ]+", value.lower())
    return [token for token in tokens if len(token) > 2 and token not in _STOPWORDS]


def extract_visual_keywords(topic: str, script: str, preset: str) -> list[str]:
    """Extract deterministic visual search hints without provider calls."""

    clean_topic = _clean_text(topic)
    clean_preset = _clean_preset(preset)
    keywords: list[str] = []

    if clean_topic:
        keywords.append(clean_topic.lower())

    preset_keywords = {
        "educational": ["checklist", "detalle", "explicacion"],
        "listicle": ["lista", "ranking", "comparacion"],
        "problem_solution": ["problema", "solucion", "riesgo"],
        "sales": ["beneficio", "decision", "valor"],
        "story": ["persona", "historia", "decision"],
    }
    keywords.extend(preset_keywords[clean_preset])

    for token in _keyword_tokens(f"{clean_topic} {script}"):
        if token not in keywords:
            keywords.append(token)
        if len(keywords) >= 8:
            break

    return keywords[:8]


def build_topic_script_plan(intent: dict[str, Any]) -> dict[str, Any]:
    """Build a local topic-to-video draft plan that still needs audio."""

    source = dict(intent or {})
    topic = _clean_text(source.get("topic"))
    preset = _clean_preset(source.get("preset"))
    duration = _duration_seconds(source.get("duration_seconds"))
    language = _language(source.get("language"))
    source_script = _clean_text(source.get("script"))

    if not topic and not source_script:
        return {
            "ok": False,
            "status": "NEEDS_INPUT",
            "reason": "missing_topic_or_script",
            "mode": "topic_to_video",
            "topic": topic,
            "script": "",
            "scenes": [],
            "visual_keywords": [],
            "next_step": "provide_topic",
        }

    script = source_script or generate_local_script(
        topic=topic,
        preset=preset,
        duration_seconds=duration,
        language=language,
    )

    visual_keywords = extract_visual_keywords(topic, script, preset)
    scenes = split_script_into_scenes(script, duration)
    for scene in scenes:
        scene["visual_keywords"] = extract_visual_keywords(
            topic,
            scene.get("text", ""),
            preset,
        )[:4]

    return {
        "ok": True,
        "status": TOPIC_PLAN_STATUS_NEEDS_AUDIO,
        "reason": TOPIC_PLAN_REASON_NEEDS_AUDIO,
        "mode": "topic_to_video",
        "topic": topic,
        "preset": preset,
        "language": language,
        "duration_seconds": duration,
        "script": script,
        "scenes": scenes,
        "visual_keywords": visual_keywords,
        "next_step": "provide_audio_or_enable_tts",
    }
