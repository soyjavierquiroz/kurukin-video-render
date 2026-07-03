import math
import os
import re
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class SubtitleRules:
    max_words_per_caption: int
    ideal_min_words_per_caption: int
    max_lines: int
    max_chars_per_line: int
    min_caption_duration: float
    max_caption_duration: float


STRONG_PUNCTUATION = ".?!;。？！；"
COMMA_PUNCTUATION = ",，、"
WEAK_ENDING_WORDS = {
    "a",
    "con",
    "de",
    "el",
    "la",
    "mi",
    "o",
    "para",
    "por",
    "que",
    "su",
    "tu",
    "y",
}
CONNECTOR_WORDS = {
    "although",
    "and",
    "aunque",
    "because",
    "but",
    "cuando",
    "entonces",
    "if",
    "pero",
    "porque",
    "si",
    "so",
    "while",
}
TIMING_RE = re.compile(
    r"^\s*(\d{1,3}:\d{2}:\d{2},\d{3})\s*-->\s*"
    r"(\d{1,3}:\d{2}:\d{2},\d{3})\s*$"
)
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def optimize_srt_file(subtitle_path: str, aspect: str = "9:16") -> dict:
    result = {
        "enabled": True,
        "changed": False,
        "original_items": 0,
        "optimized_items": 0,
        "backup_path": None,
        "aspect": _normalize_aspect(aspect),
    }

    if not subtitle_path or not os.path.isfile(subtitle_path):
        return result

    with open(subtitle_path, "r", encoding="utf-8") as file:
        original_content = file.read()

    items = parse_srt(original_content)
    result["original_items"] = len(items)
    result["optimized_items"] = len(items)
    if not items:
        return result

    rules = _rules_for_aspect(result["aspect"])
    optimized_items = []
    changed = False

    for item in items:
        start = max(0.0, item["start"])
        end = max(start, item["end"])
        original_text = item["text"]
        normalized_text = _normalize_caption_text(original_text)

        chunks = split_caption_text(normalized_text, rules)
        max_chunks_by_duration = max(
            1, math.floor((end - start) / rules.min_caption_duration)
        )
        chunks = _limit_chunks(chunks, max_chunks_by_duration, rules)
        cue_timing = redistribute_timing(start, end, chunks)

        if len(chunks) != 1 or normalized_text != original_text.strip():
            changed = True

        for chunk, (chunk_start, chunk_end) in zip(chunks, cue_timing):
            optimized_items.append(
                {
                    "start": chunk_start,
                    "end": chunk_end,
                    "text": chunk,
                }
            )

    result["optimized_items"] = len(optimized_items)
    if result["optimized_items"] != result["original_items"]:
        changed = True

    if not changed:
        return result

    backup_path = _backup_path_for(subtitle_path)
    if not os.path.exists(backup_path):
        shutil.copyfile(subtitle_path, backup_path)

    optimized_content = write_srt(optimized_items)
    temp_path = f"{subtitle_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        file.write(optimized_content)
    os.replace(temp_path, subtitle_path)

    result["changed"] = True
    result["backup_path"] = backup_path
    return result


def parse_srt(content: str):
    if not content:
        return []

    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    items = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if len(lines) < 3:
            continue

        try:
            int(lines[0].strip())
        except ValueError:
            continue

        timing_match = TIMING_RE.match(lines[1])
        if not timing_match:
            continue

        start = parse_timestamp(timing_match.group(1))
        end = parse_timestamp(timing_match.group(2))
        if end < start:
            continue

        items.append(
            {
                "start": start,
                "end": end,
                "text": "\n".join(lines[2:]).strip(),
            }
        )

    return items


def parse_timestamp(value: str) -> float:
    match = re.match(r"^(\d{1,3}):(\d{2}):(\d{2}),(\d{3})$", value.strip())
    if not match:
        raise ValueError(f"invalid SRT timestamp: {value}")

    hours, minutes, seconds, milliseconds = [int(part) for part in match.groups()]
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def format_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_milliseconds, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def split_caption_text(text: str, rules: SubtitleRules) -> list[str]:
    normalized_text = _normalize_caption_text(text)
    if not normalized_text:
        return []

    if _is_cjk_text(normalized_text):
        chunks = _split_cjk_text(normalized_text, rules)
    else:
        chunks = _split_words(normalized_text.split(), rules)

    return [_wrap_caption(chunk, rules) for chunk in chunks if chunk.strip()]


def redistribute_timing(start: float, end: float, chunks: list[str]):
    if not chunks:
        return []

    start = max(0.0, start)
    end = max(start, end)
    duration = end - start
    chunk_count = len(chunks)
    if chunk_count == 1:
        return [(start, end)]

    step = duration / chunk_count
    timing = []
    current_start = start
    for index in range(chunk_count):
        current_end = end if index == chunk_count - 1 else start + step * (index + 1)
        current_end = max(current_start, min(current_end, end))
        timing.append((current_start, current_end))
        current_start = current_end

    return timing


def write_srt(items) -> str:
    blocks = []
    for index, item in enumerate(items, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_timestamp(item['start'])} --> {format_timestamp(item['end'])}",
                    item["text"].strip(),
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _normalize_aspect(aspect) -> str:
    value = getattr(aspect, "value", aspect)
    value = str(value or "9:16")
    if value == "16:9" or "landscape" in value.lower():
        return "16:9"
    return "9:16"


def _rules_for_aspect(aspect: str) -> SubtitleRules:
    if aspect == "16:9":
        return SubtitleRules(
            max_words_per_caption=8,
            ideal_min_words_per_caption=3,
            max_lines=2,
            max_chars_per_line=34,
            min_caption_duration=0.9,
            max_caption_duration=4.0,
        )

    return SubtitleRules(
        max_words_per_caption=5,
        ideal_min_words_per_caption=2,
        max_lines=2,
        max_chars_per_line=22,
        min_caption_duration=0.75,
        max_caption_duration=2.8,
    )


def _normalize_caption_text(text: str) -> str:
    return " ".join((text or "").split())


def _is_cjk_text(text: str) -> bool:
    compact_text = re.sub(r"\s+", "", text)
    if not compact_text:
        return False
    cjk_chars = CJK_RE.findall(compact_text)
    return bool(cjk_chars) and len(cjk_chars) / len(compact_text) >= 0.35


def _split_words(words: list[str], rules: SubtitleRules) -> list[str]:
    if len(words) <= rules.max_words_per_caption:
        return [" ".join(words)]

    chunks = []
    start = 0
    while start < len(words):
        end = min(start + rules.max_words_per_caption, len(words))
        if end < len(words):
            end = _choose_word_cut(words, start, end, rules)
        chunks.append(" ".join(words[start:end]))
        start = end

    return chunks


def _choose_word_cut(
    words: list[str], start: int, end: int, rules: SubtitleRules
) -> int:
    min_end = min(len(words), start + rules.ideal_min_words_per_caption)
    for punctuation in (STRONG_PUNCTUATION, COMMA_PUNCTUATION):
        for index in range(end - 1, min_end - 2, -1):
            if words[index].rstrip().endswith(tuple(punctuation)):
                return index + 1

    for index in range(end - 1, min_end - 1, -1):
        word = _clean_word(words[index])
        if word in CONNECTOR_WORDS:
            return index

    chosen = end
    if chosen < len(words) and _clean_word(words[chosen - 1]) in WEAK_ENDING_WORDS:
        if chosen - start < rules.max_words_per_caption:
            chosen += 1
        elif chosen - 1 >= min_end:
            chosen -= 1

    return max(min_end, chosen)


def _split_cjk_text(text: str, rules: SubtitleRules) -> list[str]:
    max_chars = rules.max_chars_per_line
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        window = remaining[:max_chars]
        cut = _find_cjk_cut(window)
        if cut <= 0:
            cut = max_chars

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    return chunks


def _find_cjk_cut(text: str) -> int:
    for punctuation in (STRONG_PUNCTUATION, COMMA_PUNCTUATION):
        positions = [text.rfind(char) for char in punctuation]
        position = max(positions)
        if position >= 0:
            return position + 1
    return -1


def _wrap_caption(text: str, rules: SubtitleRules) -> str:
    text = _normalize_caption_text(text)
    if len(text) <= rules.max_chars_per_line:
        return text

    if _is_cjk_text(text):
        return _wrap_cjk_caption(text, rules)
    return _wrap_word_caption(text, rules)


def _wrap_word_caption(text: str, rules: SubtitleRules) -> str:
    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        candidate = f"{current_line} {word}".strip()
        if current_line and len(candidate) > rules.max_chars_per_line:
            lines.append(current_line)
            current_line = word
        else:
            current_line = candidate

    if current_line:
        lines.append(current_line)

    if len(lines) <= rules.max_lines:
        return "\n".join(lines)

    head = lines[: rules.max_lines - 1]
    tail = " ".join(lines[rules.max_lines - 1 :])
    return "\n".join([*head, tail])


def _wrap_cjk_caption(text: str, rules: SubtitleRules) -> str:
    if len(text) <= rules.max_chars_per_line:
        return text

    lines = [
        text[index : index + rules.max_chars_per_line]
        for index in range(0, len(text), rules.max_chars_per_line)
    ]
    if len(lines) <= rules.max_lines:
        return "\n".join(lines)

    return "\n".join(
        [
            *lines[: rules.max_lines - 1],
            "".join(lines[rules.max_lines - 1 :]),
        ]
    )


def _limit_chunks(chunks: list[str], max_chunks: int, rules: SubtitleRules) -> list[str]:
    chunks = [chunk for chunk in chunks if chunk.strip()]
    if len(chunks) <= max_chunks:
        return chunks

    limited = chunks[:]
    while len(limited) > max_chunks:
        merge_index = _shortest_merge_index(limited)
        merged = _normalize_caption_text(
            f"{limited[merge_index]} {limited[merge_index + 1]}"
        )
        limited[merge_index : merge_index + 2] = [_wrap_caption(merged, rules)]

    return limited


def _shortest_merge_index(chunks: list[str]) -> int:
    best_index = 0
    best_size = None
    for index in range(len(chunks) - 1):
        size = len(_normalize_caption_text(chunks[index])) + len(
            _normalize_caption_text(chunks[index + 1])
        )
        if best_size is None or size < best_size:
            best_size = size
            best_index = index
    return best_index


def _clean_word(word: str) -> str:
    return word.strip(" \t\r\n.,;:!?¡¿\"'()[]{}").lower()


def _backup_path_for(subtitle_path: str) -> str:
    directory = os.path.dirname(subtitle_path)
    return os.path.join(directory, "subtitle.original.srt")
