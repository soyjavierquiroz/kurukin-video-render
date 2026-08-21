import json
import os.path
import re
import shutil
import unicodedata
from difflib import SequenceMatcher
from timeit import default_timer as timer

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
from loguru import logger

from app.config import config
from app.utils import utils

model_size = config.whisper.get("model_size", "large-v3")
device = config.whisper.get("device", "cpu")
compute_type = config.whisper.get("compute_type", "int8")
initial_prompt = config.whisper.get("initial_prompt", "") or None
model = None
GLOBAL_OK_THRESHOLD = 0.90
LINE_MIN_COVERAGE = 0.40
SEMANTIC_SUBTITLE_TARGET_MIN_CHARS = 20
SEMANTIC_SUBTITLE_TARGET_MAX_CHARS = 36
SEMANTIC_SUBTITLE_SOFT_CUE_CHARS = 42
SEMANTIC_SUBTITLE_HARD_CUE_CHARS = 48
SEMANTIC_SUBTITLE_TARGET_MIN_WORDS = 4
SEMANTIC_SUBTITLE_TARGET_MAX_WORDS = 7
SEMANTIC_BOUNDARY_CONNECTORS = {
    "ademas",
    "además",
    "aunque",
    "como",
    "cuando",
    "donde",
    "entonces",
    "luego",
    "mientras",
    "ni",
    "pero",
    "porque",
    "que",
    "sin",
    "si",
    "tambien",
    "también",
    "y",
}
SEMANTIC_ORPHAN_WORDS = {
    "aunque",
    "con",
    "de",
    "del",
    "el",
    "la",
    "las",
    "los",
    "o",
    "para",
    "pero",
    "por",
    "que",
    "sin",
    "y",
}


def create(audio_file, subtitle_file: str = ""):
    global model
    if WhisperModel is None:
        logger.warning("faster_whisper not available, skipping whisper subtitle generation")
        return ""
    if not model:
        model_path = f"{utils.root_dir()}/models/whisper-{model_size}"
        model_bin_file = f"{model_path}/model.bin"
        if not os.path.isdir(model_path) or not os.path.isfile(model_bin_file):
            model_path = model_size

        logger.info(
            f"loading model: {model_path}, device: {device}, compute_type: {compute_type}"
        )
        try:
            model = WhisperModel(
                model_size_or_path=model_path, device=device, compute_type=compute_type
            )
        except Exception as e:
            logger.error(
                f"failed to load model: {e} \n\n"
                f"********************************************\n"
                f"this may be caused by network issue. \n"
                f"please download the model manually and put it in the 'models' folder. \n"
                f"see [README.md FAQ](https://github.com/harry0703/MoneyPrinterTurbo) for more details.\n"
                f"********************************************\n\n"
            )
            return None

    logger.info(f"start, output file: {subtitle_file}")
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"

    segments, info = model.transcribe(
        audio_file,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        **({"initial_prompt": initial_prompt} if initial_prompt else {}),
    )

    logger.info(
        f"detected language: '{info.language}', probability: {info.language_probability:.2f}"
    )

    start = timer()
    subtitles = []

    def recognized(seg_text, seg_start, seg_end):
        seg_text = seg_text.strip()
        if not seg_text:
            return

        msg = "[%.2fs -> %.2fs] %s" % (seg_start, seg_end, seg_text)
        logger.debug(msg)

        subtitles.append(
            {"msg": seg_text, "start_time": seg_start, "end_time": seg_end}
        )

    for segment in segments:
        words_idx = 0
        words_len = len(segment.words)

        seg_start = 0
        seg_end = 0
        seg_text = ""

        if segment.words:
            is_segmented = False
            for word in segment.words:
                if not is_segmented:
                    seg_start = word.start
                    is_segmented = True

                seg_end = word.end
                # If it contains punctuation, then break the sentence.
                seg_text += word.word

                if utils.str_contains_punctuation(word.word):
                    # remove last char
                    seg_text = seg_text[:-1]
                    if not seg_text:
                        continue

                    recognized(seg_text, seg_start, seg_end)

                    is_segmented = False
                    seg_text = ""

                if words_idx == 0 and segment.start < word.start:
                    seg_start = word.start
                if words_idx == (words_len - 1) and segment.end > word.end:
                    seg_end = word.end
                words_idx += 1

        if not seg_text:
            continue

        recognized(seg_text, seg_start, seg_end)

    end = timer()

    diff = end - start
    logger.info(f"complete, elapsed: {diff:.2f} s")

    idx = 1
    lines = []
    for subtitle in subtitles:
        text = subtitle.get("msg")
        if text:
            lines.append(
                utils.text_to_srt(
                    idx, text, subtitle.get("start_time"), subtitle.get("end_time")
                )
            )
            idx += 1

    sub = "\n".join(lines) + "\n"
    with open(subtitle_file, "w", encoding="utf-8") as f:
        f.write(sub)
    logger.info(f"subtitle file created: {subtitle_file}")


def file_to_subtitles(filename):
    if not filename or not os.path.isfile(filename):
        return []

    times_texts = []
    current_times = None
    current_text = ""
    index = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            times = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", line)
            if times:
                current_times = line
            elif line.strip() == "" and current_times:
                index += 1
                times_texts.append((index, current_times.strip(), current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line

    # Flush the final block. SRT files whose last subtitle is not followed by a
    # trailing blank line never hit the blank-line branch above, so without this
    # the last subtitle would be silently dropped.
    if current_times:
        index += 1
        times_texts.append((index, current_times.strip(), current_text.strip()))
    return times_texts


def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity(a, b):
    distance = levenshtein_distance(a.lower(), b.lower())
    max_length = max(len(a), len(b))
    return 1 - (distance / max_length)


def _subtitle_raw_path(subtitle_file):
    base, ext = os.path.splitext(subtitle_file)
    return f"{base}.raw{ext or '.srt'}"


def _subtitle_alignment_report_path(subtitle_file):
    base, _ = os.path.splitext(subtitle_file)
    return f"{base}-alignment.json"


def _parse_srt_time(time_value):
    hours, minutes, seconds, milliseconds = re.match(
        r"(\d+):(\d+):(\d+),(\d+)", time_value
    ).groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000
    )


def _parse_srt_timerange(timerange):
    start_text, end_text = timerange.split(" --> ", 1)
    return _parse_srt_time(start_text), _parse_srt_time(end_text)


def _normalize_token_for_subtitle_alignment(text):
    text = unicodedata.normalize("NFKD", text.lower())
    chars = []
    for char in text:
        if unicodedata.combining(char):
            continue
        if char.isalnum():
            chars.append(char)
    return "".join(chars)


def _tokenize_for_subtitle_alignment(text):
    tokens = []
    current = []
    for char in text or "":
        if char.isalnum():
            current.append(char)
        elif current:
            token = _normalize_token_for_subtitle_alignment("".join(current))
            if token:
                tokens.append(token)
            current = []
    if current:
        token = _normalize_token_for_subtitle_alignment("".join(current))
        if token:
            tokens.append(token)
    return tokens


def _whisper_subtitle_tokens(subtitle_items):
    tokens = []
    for subtitle_index, item in enumerate(subtitle_items):
        try:
            start, end = _parse_srt_timerange(item[1])
        except (AttributeError, ValueError):
            continue
        text_tokens = _tokenize_for_subtitle_alignment(item[2])
        if not text_tokens or end <= start:
            continue

        token_duration = (end - start) / len(text_tokens)
        for token_index, token in enumerate(text_tokens):
            token_start = start + token_duration * token_index
            token_end = (
                end
                if token_index == len(text_tokens) - 1
                else start + token_duration * (token_index + 1)
            )
            tokens.append(
                {
                    "text": token,
                    "start": max(0, token_start),
                    "end": max(0, token_end),
                    "subtitle_index": subtitle_index,
                }
            )
    return tokens


def _script_subtitle_lines(video_script):
    normalized_script = utils.normalize_script_for_subtitle_matching(video_script)
    canonical_lines = utils.split_string_by_punctuations(normalized_script)
    script_lines = _split_script_lines_preserving_punctuation(normalized_script)
    if len(script_lines) != len(canonical_lines):
        logger.debug(
            "subtitle script split count differs after preserving punctuation, "
            f"canonical: {len(canonical_lines)}, preserved: {len(script_lines)}"
        )
    lines = []
    flat_tokens = []
    for line in script_lines:
        original_text = line.strip()
        tokens = _tokenize_for_subtitle_alignment(original_text)
        token_indices = []
        for token in tokens:
            token_indices.append(len(flat_tokens))
            flat_tokens.append(token)
        lines.append(
            {
                "text": original_text,
                "tokens": tokens,
                "token_indices": token_indices,
            }
        )
    return lines, flat_tokens


def _split_script_lines_preserving_punctuation(text):
    result = []
    current = ""

    for index, char in enumerate(text or ""):
        previous_char = text[index - 1] if index > 0 else ""
        next_char = text[index + 1] if index < len(text) - 1 else ""

        if char == "\n":
            if current.strip():
                result.append(current.strip())
            current = ""
            continue

        current += char

        if char == "." and previous_char.isdigit() and next_char.isdigit():
            continue
        if char == "," and previous_char.isdigit() and next_char.isdigit():
            continue
        if utils.str_contains_punctuation(char):
            if current.strip():
                result.append(current.strip())
            current = ""

    if current.strip():
        result.append(current.strip())
    return result


def _align_script_to_whisper(script_tokens, whisper_tokens):
    whisper_text_tokens = [
        token["text"]
        for token in whisper_tokens
    ]

    matcher = SequenceMatcher(
        None,
        script_tokens,
        whisper_text_tokens,
        autojunk=False,
    )

    mapping = {}
    replace_blocks = []

    # First preserve ordinary exact token matches.
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset] = j1 + offset
        elif tag == "replace":
            replace_blocks.append(
                (i1, i2, j1, j2)
            )

    # Whisper can legitimately merge adjacent words which are
    # separate tokens in the canonical script:
    #
    #   "descansar", "te"  -> "descansarte"
    #   "recibir", "te"    -> "recibirte"
    #
    # Treat those word-boundary differences as exact matches.
    # Multiple script tokens may therefore point to the same
    # Whisper token/timing anchor.
    for i1, i2, j1, j2 in replace_blocks:
        used_whisper_indices = set(
            mapping.values()
        )

        for whisper_index in range(j1, j2):
            if whisper_index in used_whisper_indices:
                continue

            whisper_token = whisper_text_tokens[
                whisper_index
            ]

            matched = False

            # Two-token merges cover the common Spanish clitic
            # cases. Three-token support costs little and makes
            # the rule robust without fuzzy matching.
            for group_size in (2, 3):
                max_start = i2 - group_size + 1

                for script_index in range(
                    i1,
                    max_start,
                ):
                    indices = list(
                        range(
                            script_index,
                            script_index + group_size,
                        )
                    )

                    if any(
                        index in mapping
                        for index in indices
                    ):
                        continue

                    combined = "".join(
                        script_tokens[index]
                        for index in indices
                    )

                    if combined != whisper_token:
                        continue

                    for index in indices:
                        mapping[index] = whisper_index

                    used_whisper_indices.add(
                        whisper_index
                    )

                    matched = True
                    break

                if matched:
                    break

    return mapping


def _plain_words(text):
    return re.findall(r"\S+", text or "")


def _semantic_boundary_strength(left_word, right_word=""):
    left = str(left_word or "").strip()
    right = str(right_word or "").strip()
    left_token = _normalize_token_for_subtitle_alignment(left)
    right_token = _normalize_token_for_subtitle_alignment(right)
    if re.search(r"[.!?！？。]$", left):
        return 100
    if re.search(r"[;:]$", left):
        return 80
    if left.endswith(","):
        return 60
    if right_token in SEMANTIC_BOUNDARY_CONNECTORS:
        return 45
    if left_token in SEMANTIC_BOUNDARY_CONNECTORS:
        return 35
    return 5


def _is_semantic_orphan(word):
    return _normalize_token_for_subtitle_alignment(word) in SEMANTIC_ORPHAN_WORDS


def _join_semantic_words(words):
    return " ".join(word["text"] for word in words)


def _semantic_words_from_alignment(script_lines, token_mapping, whisper_tokens):
    words = []
    for script_line in script_lines:
        token_offset = 0
        for raw_word in _plain_words(script_line["text"]):
            word_tokens = _tokenize_for_subtitle_alignment(raw_word)
            token_indices = script_line["token_indices"][
                token_offset: token_offset + len(word_tokens)
            ]
            token_offset += len(word_tokens)
            mapped = [
                token_mapping[token_index]
                for token_index in token_indices
                if token_index in token_mapping
            ]
            if mapped:
                first = whisper_tokens[min(mapped)]
                last = whisper_tokens[max(mapped)]
                start = max(0, first["start"])
                end = max(0, last["end"])
            else:
                start = None
                end = None
            words.append(
                {
                    "text": raw_word,
                    "token_indices": token_indices,
                    "mapped_indices": mapped,
                    "start": start,
                    "end": end,
                }
            )
    return words


def _cue_end_score(words, start, end, total_words):
    selected = words[start:end]
    text = _join_semantic_words(selected)
    if not selected:
        return None
    if len(text) > SEMANTIC_SUBTITLE_HARD_CUE_CHARS:
        return None
    next_word = words[end]["text"] if end < total_words else ""
    boundary = _semantic_boundary_strength(selected[-1]["text"], next_word)
    word_count = len(selected)
    target_midpoint = (
        SEMANTIC_SUBTITLE_TARGET_MIN_CHARS + SEMANTIC_SUBTITLE_TARGET_MAX_CHARS
    ) / 2
    fullness = -abs(len(text) - target_midpoint)
    if (
        SEMANTIC_SUBTITLE_TARGET_MIN_CHARS
        <= len(text)
        <= SEMANTIC_SUBTITLE_TARGET_MAX_CHARS
    ):
        fullness += 45
    if (
        SEMANTIC_SUBTITLE_TARGET_MIN_WORDS
        <= word_count
        <= SEMANTIC_SUBTITLE_TARGET_MAX_WORDS
    ):
        fullness += 35
    elif word_count == 1 and end < total_words:
        fullness -= 500 if _is_semantic_orphan(selected[0]["text"]) else 180
    orphan_penalty = 0
    if _is_semantic_orphan(selected[-1]["text"]) and end < total_words:
        orphan_penalty += 80
    if end < total_words and (total_words - end) == 1:
        orphan_penalty += 120
    internal_boundary_penalty = 0
    for word in selected[:-1]:
        if _semantic_boundary_strength(word["text"]) >= 60:
            internal_boundary_penalty += 180
    final_bonus = 40 if end == total_words else 0
    return (
        boundary * 10
        + fullness
        + final_bonus
        - orphan_penalty
        - internal_boundary_penalty
    )


def _candidate_cue_end(words, start, total_words):
    max_end = start + 1
    for end in range(start + 1, total_words + 1):
        text = _join_semantic_words(words[start:end])
        if len(text) > SEMANTIC_SUBTITLE_HARD_CUE_CHARS:
            break
        max_end = end

    if max_end <= start:
        return min(total_words, start + 1)

    candidates = list(range(start + 1, max_end + 1))
    multi_word_candidates = [
        end
        for end in candidates
        if end == total_words or len(words[start:end]) > 1
    ]
    if multi_word_candidates:
        candidates = multi_word_candidates
    for minimum_strength in (100, 80, 60, 35):
        matching = [
            end
            for end in candidates
            if _semantic_boundary_strength(
                words[end - 1]["text"],
                words[end]["text"] if end < total_words else "",
            )
            >= minimum_strength
            and (end == total_words or len(words[start:end]) > 1)
            and (end == total_words or (total_words - end) != 1)
        ]
        if matching:
            return max(
                matching,
                key=lambda candidate: _cue_end_score(
                    words, start, candidate, total_words
                ),
            )

    return max(
        candidates,
        key=lambda candidate: _cue_end_score(words, start, candidate, total_words),
    )


def _span_is_complete_short_sentence(words, start, end):
    text = _join_semantic_words(words[start:end])
    return bool(re.search(r"[.!?！？。]$", text)) and len(words[start:end]) <= 2


def _balanced_span_score(words, start, end, total_words):
    selected = words[start:end]
    if not selected:
        return None
    text = _join_semantic_words(selected)
    char_count = len(text)
    if char_count > SEMANTIC_SUBTITLE_HARD_CUE_CHARS:
        return None

    word_count = len(selected)
    next_word = words[end]["text"] if end < total_words else ""
    boundary = _semantic_boundary_strength(selected[-1]["text"], next_word)
    target_chars = (
        SEMANTIC_SUBTITLE_TARGET_MIN_CHARS
        + SEMANTIC_SUBTITLE_TARGET_MAX_CHARS
    ) / 2
    target_words = (
        SEMANTIC_SUBTITLE_TARGET_MIN_WORDS
        + SEMANTIC_SUBTITLE_TARGET_MAX_WORDS
    ) / 2
    score = boundary * 4
    score -= abs(char_count - target_chars) * 5
    score -= abs(word_count - target_words) * 16

    if SEMANTIC_SUBTITLE_TARGET_MIN_CHARS <= char_count <= SEMANTIC_SUBTITLE_TARGET_MAX_CHARS:
        score += 90
    elif char_count <= SEMANTIC_SUBTITLE_SOFT_CUE_CHARS:
        score += 35
    else:
        score -= 140

    if SEMANTIC_SUBTITLE_TARGET_MIN_WORDS <= word_count <= SEMANTIC_SUBTITLE_TARGET_MAX_WORDS:
        score += 90
    elif word_count <= 2 and not _span_is_complete_short_sentence(words, start, end):
        score -= 700
    elif word_count == 3:
        score -= 45

    if end < total_words and _is_semantic_orphan(selected[-1]["text"]):
        score -= 180
    if end < total_words and _is_semantic_orphan(next_word) and (total_words - end) <= 2:
        score -= 260

    internal_boundary_penalty = 0
    for word in selected[:-1]:
        if _semantic_boundary_strength(word["text"]) >= 60:
            internal_boundary_penalty += 260
    score -= internal_boundary_penalty

    if end == total_words:
        score += 120 if re.search(r"[.!?！？。]$", selected[-1]["text"]) else 30

    return score


def _balanced_cue_spans(words):
    total_words = len(words)
    if total_words == 0:
        return []

    best: list[tuple[float, list[tuple[int, int]] | None]] = [(-10**12, None) for _ in range(total_words + 1)]
    best[total_words] = (0.0, [])

    for start in range(total_words - 1, -1, -1):
        for end in range(start + 1, total_words + 1):
            text = _join_semantic_words(words[start:end])
            if len(text) > SEMANTIC_SUBTITLE_HARD_CUE_CHARS:
                break
            span_score = _balanced_span_score(words, start, end, total_words)
            if span_score is None:
                continue
            tail_score, tail_spans = best[end]
            if tail_spans is None:
                continue
            candidate_score = span_score + tail_score
            if candidate_score > best[start][0]:
                best[start] = (candidate_score, [(start, end), *tail_spans])

    if best[0][1] is not None:
        return best[0][1]

    spans = []
    start = 0
    while start < total_words:
        end = _candidate_cue_end(words, start, total_words)
        spans.append((start, end))
        start = end
    return spans


def _semantic_subtitle_items(script_lines, token_mapping, whisper_tokens):
    words = _semantic_words_from_alignment(script_lines, token_mapping, whisper_tokens)
    output = []
    index = 1
    previous_end = 0

    for start, end in _balanced_cue_spans(words):
        selected = words[start:end]
        mapped_words = [
            word
            for word in selected
            if word["start"] is not None and word["end"] is not None
        ]
        if not mapped_words:
            start = end
            continue

        cue_start = max(previous_end, mapped_words[0]["start"])
        cue_end = mapped_words[-1]["end"]
        if cue_end <= cue_start:
            start = end
            continue

        output.append((index, cue_start, cue_end, _join_semantic_words(selected)))
        previous_end = cue_end
        index += 1

    return output


def _build_alignment_result(subtitle_file, video_script):
    subtitle_items = file_to_subtitles(subtitle_file)
    whisper_tokens = _whisper_subtitle_tokens(subtitle_items)
    script_lines, script_tokens = _script_subtitle_lines(video_script)
    token_mapping = _align_script_to_whisper(script_tokens, whisper_tokens)

    total_script_tokens = len(script_tokens)
    matched_script_tokens = len(token_mapping)
    script_coverage = (
        matched_script_tokens / total_script_tokens if total_script_tokens else 0
    )

    report_lines = []
    line_output_items = []
    all_lines_have_min_coverage = True
    all_lines_have_valid_timing = True
    monotonic = True
    previous_end = 0

    for line_index, script_line in enumerate(script_lines, start=1):
        mapped_token_indices = [
            token_mapping[token_index]
            for token_index in script_line["token_indices"]
            if token_index in token_mapping
        ]
        total_line_tokens = len(script_line["tokens"])
        matched_line_tokens = len(mapped_token_indices)
        line_coverage = (
            matched_line_tokens / total_line_tokens if total_line_tokens else 0
        )

        start = None
        end = None
        if mapped_token_indices:
            first_token = whisper_tokens[min(mapped_token_indices)]
            last_token = whisper_tokens[max(mapped_token_indices)]
            start = max(0, first_token["start"])
            end = max(0, last_token["end"])

            if start < previous_end:
                start = previous_end
            if end <= start:
                all_lines_have_valid_timing = False
            else:
                previous_end = end
                line_output_items.append((line_index, start, end, script_line["text"]))
        else:
            all_lines_have_valid_timing = False

        if total_line_tokens and line_coverage < LINE_MIN_COVERAGE:
            all_lines_have_min_coverage = False
        if start is not None and end is not None and end < start:
            monotonic = False

        report_lines.append(
            {
                "index": line_index,
                "text": script_line["text"],
                "matched_tokens": matched_line_tokens,
                "total_tokens": total_line_tokens,
                "coverage": round(line_coverage, 4),
                "start": round(start, 3) if start is not None else None,
                "end": round(end, 3) if end is not None else None,
            }
        )

    status_ok = (
        total_script_tokens > 0
        and script_coverage >= GLOBAL_OK_THRESHOLD
        and all_lines_have_min_coverage
        and all_lines_have_valid_timing
        and monotonic
        and len(line_output_items) == len(script_lines)
    )
    status = "ok" if status_ok else "review_required"
    output_items = (
        _semantic_subtitle_items(script_lines, token_mapping, whisper_tokens)
        if status_ok
        else line_output_items
    )

    return {
        "status": status,
        "confidence": round(script_coverage, 4),
        "matched_script_tokens": matched_script_tokens,
        "total_script_tokens": total_script_tokens,
        "script_coverage": round(script_coverage, 4),
        "script_lines": len(script_lines),
        "aligned_lines": len(line_output_items),
        "subtitle_cues": len(output_items),
        "review_required": not status_ok,
        "lines": report_lines,
        "_output_items": output_items,
    }


def _write_alignment_report(subtitle_file, report):
    report_path = _subtitle_alignment_report_path(subtitle_file)
    public_report = {
        key: value for key, value in report.items() if not key.startswith("_")
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(public_report, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_aligned_subtitle(subtitle_file, output_items):
    lines = []
    for index, start, end, text in output_items:
        lines.append(utils.text_to_srt(index, text, start, end).strip())
    with open(subtitle_file, "w", encoding="utf-8") as f:
        f.write("\n\n".join(lines) + "\n")


def correct(subtitle_file, video_script):
    raw_subtitle_file = _subtitle_raw_path(subtitle_file)
    if os.path.isfile(subtitle_file):
        shutil.copyfile(subtitle_file, raw_subtitle_file)

    report = _build_alignment_result(subtitle_file, video_script)
    _write_alignment_report(subtitle_file, report)

    if report["status"] == "ok":
        _write_aligned_subtitle(subtitle_file, report["_output_items"])
        logger.info(
            "subtitle aligned with script, "
            f"confidence: {report['confidence']:.4f}, "
            f"lines: {report['aligned_lines']}/{report['script_lines']}"
        )
    else:
        logger.warning(
            "subtitle alignment requires review; keeping Whisper output, "
            f"confidence: {report['confidence']:.4f}, "
            f"lines: {report['aligned_lines']}/{report['script_lines']}"
        )

    return {key: value for key, value in report.items() if not key.startswith("_")}


if __name__ == "__main__":
    task_id = "c12fd1e6-4b0a-4d65-a075-c87abe35a072"
    task_dir = utils.task_dir(task_id)
    subtitle_file = f"{task_dir}/subtitle.srt"
    audio_file = f"{task_dir}/audio.mp3"

    subtitles = file_to_subtitles(subtitle_file)
    print(subtitles)

    script_file = f"{task_dir}/script.json"
    with open(script_file, "r") as f:
        script_content = f.read()
    s = json.loads(script_content)
    script = s.get("script")

    correct(subtitle_file, script)

    subtitle_file = f"{task_dir}/subtitle-test.srt"
    create(audio_file, subtitle_file)
