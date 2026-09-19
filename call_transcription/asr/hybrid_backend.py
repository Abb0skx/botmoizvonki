from __future__ import annotations

from .base import TranscriptionBackend
from .faster_whisper_backend import FasterWhisperBackend
from ..models import ASRSegment, Word
from ..processing import language_evidence, normalize_text
from ..processing import normalized_words

_RU_TRANSLITERATION = set("zdravstvuyte zdravstvuite zdrastvuyte podskazhite podkazite pojaluysta pozhaluysta skolko kakiye kakie tsvet tsveta tsvetlar chorny chorniy cherniy spasibo bolshe xorosho horosho ponyatno vosem nalichii naliki".split())


def _candidate(segments, language, context_terms=()):
    text = normalize_text(" ".join(segment.text for segment in segments))
    confidences = [
        word.confidence
        for segment in segments
        for word in segment.words
        if word.confidence is not None
    ] or [segment.confidence for segment in segments if segment.confidence is not None]
    # GigaAM does not provide Whisper-style posterior scores. Missing scores
    # must not automatically lose to a confident but wrong Whisper hypothesis.
    confidence = sum(confidences) / len(confidences) if confidences else 0.65
    evidence = language_evidence(text, context_terms)
    lexical = evidence[language]
    script = evidence["cyrillic" if language == "ru" else "latin"]
    score = confidence * 2.0 + min(evidence["words"], 8) * 0.04 + lexical * 0.45 + script * 0.06
    if language == "uz":
        score -= 1.1 * len(set(normalized_words(text)) & _RU_TRANSLITERATION)
    return {
        "segments": segments,
        "text": text,
        "confidence": confidence,
        "evidence": evidence,
        "score": score,
    }


def choose_ru_uz(russian_segments, uzbek_segments, context_terms=()):
    """Choose a transcript without treating another language as valid output.

    Scores are intentionally conservative rather than claimed probabilities.
    Russian Cyrillic plus common RU words is strong evidence. Uzbek needs Uzbek
    lexical evidence or markedly better coverage/confidence; arbitrary Latin
    product names alone do not force an Uzbek decision.
    """
    ru = _candidate(russian_segments, "ru", context_terms)
    uz = _candidate(uzbek_segments, "uz", context_terms)
    if not ru["text"]:
        return uzbek_segments
    if not uz["text"]:
        return russian_segments

    ru_ev, uz_ev = ru["evidence"], uz["evidence"]
    transliterated_ru = len(set(normalized_words(uz["text"])) & _RU_TRANSLITERATION)
    if transliterated_ru and ru_ev["ru"] >= 1 and ru_ev["cyrillic"] >= 2 and uz_ev["uz"] <= 1:
        return russian_segments
    if ru_ev["ru"] >= 1 and ru_ev["cyrillic"] >= 2 and uz_ev["uz"] == 0:
        return russian_segments
    if uz_ev["uz"] >= 2 and ru_ev["ru"] == 0:
        return uzbek_segments
    if (uz_ev["uz"] >= 1 and uz_ev["latin"] >= 2
            and (ru_ev["ru"] == 0 or uz["score"] >= ru["score"] - 0.15)):
        return uzbek_segments
    return uzbek_segments if uz["score"] > ru["score"] + 0.2 else russian_segments


def _slice_segments(segments, start, end, language):
    words = [
        word
        for segment in segments
        for word in segment.words
        if start <= (word.start + word.end) / 2 < end
    ]
    if words:
        text = normalize_text(" ".join(word.text for word in words))
        confidence_values = [word.confidence for word in words if word.confidence is not None]
        confidence = sum(confidence_values) / len(confidence_values) if confidence_values else None
        safe_words = [Word(word.start, word.end, word.text, word.confidence) for word in words]
        return [ASRSegment(
            safe_words[0].start, safe_words[-1].end, text, safe_words,
            confidence=confidence, language=language,
        )]
    overlapping = [
        segment for segment in segments
        if start <= (segment.start + segment.end) / 2 < end
    ]
    return overlapping


def choose_ru_uz_by_time(russian_segments, uzbek_segments, *, window_seconds=6.0):
    """Route bounded time slices after running each model once per ASR window."""
    all_segments = [*russian_segments, *uzbek_segments]
    if not all_segments:
        return []
    first = min(segment.start for segment in all_segments)
    last = max(segment.end for segment in all_segments)
    selected = []
    start = first
    while start < last:
        end = min(last + 1e-6, start + window_seconds)
        ru = _slice_segments(russian_segments, start, end, "ru")
        uz = _slice_segments(uzbek_segments, start, end, "uz")
        choice = choose_ru_uz(ru, uz)
        language = "uz" if choice is uz else "ru"
        for segment in choice:
            segment.language = language
        selected.extend(choice)
        start = end
    return sorted(selected, key=lambda segment: (segment.start, segment.end))


def choose_ru_uz_by_phrases(russian_segments, uzbek_segments):
    """Route complete short spans, keeping each model's word order intact.

    Prefer actual pauses shared by both hypotheses. Limit spans to ~3 seconds
    for within-sentence language switches. Never transliterate a recognized
    word or manufacture a catalogue name to make the output look plausible.
    """
    units = sorted(
        [word for segment in [*russian_segments, *uzbek_segments]
         for word in (segment.words or [segment])], key=lambda word: word.start,
    )
    if not units:
        return []
    first, last = units[0].start, max(w.end for w in units)
    boundaries = [first]
    covered_until = first
    for unit in units:
        if unit.start - covered_until >= 0.2 and covered_until - boundaries[-1] >= 0.7:
            boundaries.append((covered_until + unit.start) / 2)
        elif unit.start - boundaries[-1] >= 3.0:
            boundaries.append(unit.start)
        covered_until = max(covered_until, unit.end)
    boundaries.append(last + 1e-6)
    selected = []
    for start, end in zip(boundaries, boundaries[1:]):
        ru = _slice_segments(russian_segments, start, end, "ru")
        uz = _slice_segments(uzbek_segments, start, end, "uz")
        choice = choose_ru_uz(ru, uz)
        for segment in choice:
            segment.language = "uz" if choice is uz else "ru"
        selected.extend(choice)
    return sorted(selected, key=lambda segment: (segment.start, segment.end))


class HybridRUUZBackend(TranscriptionBackend):
    """Russian GigaAM/Whisper + telephone-tuned Uzbek Whisper by short phrase.

    The two models run sequentially. This costs more CPU than one multilingual
    pass, but keeps the proven Russian recognizer and gives Uzbek calls a model
    explicitly fine-tuned for narrowband/noisy call-centre audio.
    """

    def __init__(self, config, *, russian_backend=None, uzbek_backend=None):
        self.config = config
        if russian_backend is not None:
            self.russian = russian_backend
        elif config.russian_engine == "gigaam":
            from .gigaam_backend import GigaAMBackend
            self.russian = GigaAMBackend(config, model_name="v3_rnnt")
        else:
            self.russian = FasterWhisperBackend(config, language="ru")
        self.uzbek = uzbek_backend or FasterWhisperBackend(
            config,
            language="uz",
            model_path=config.uzbek_model_path,
            # This fine-tuned checkpoint was trained with anti-hallucination
            # decoding and is materially less stable with a long RU/catalog
            # prompt. Keep its recommended prompt-free decode.
            use_initial_prompt=False,
        )

    def transcribe(self, audio_path, *, start, end, initial_prompt):
        # Sequential execution keeps peak transient memory lower than parallel inference.
        uzbek = self.uzbek.transcribe(
            audio_path, start=start, end=end, initial_prompt=initial_prompt,
        )
        russian = self.russian.transcribe(
            audio_path, start=start, end=end, initial_prompt=initial_prompt,
        )
        return choose_ru_uz_by_phrases(russian, uzbek)
