from __future__ import annotations

from .base import TranscriptionBackend
from .faster_whisper_backend import FasterWhisperBackend
from .vosk_backend import VoskUzbekBackend
from ..models import ASRSegment, Word
from ..processing import language_evidence, normalize_text


def _candidate(segments, language, context_terms=()):
    text = normalize_text(" ".join(segment.text for segment in segments))
    confidences = [
        word.confidence
        for segment in segments
        for word in segment.words
        if word.confidence is not None
    ] or [segment.confidence for segment in segments if segment.confidence is not None]
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    evidence = language_evidence(text, context_terms)
    lexical = evidence[language]
    script = evidence["cyrillic" if language == "ru" else "latin"]
    score = confidence * 2.0 + min(evidence["words"], 8) * 0.04 + lexical * 0.45 + script * 0.06
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


class HybridRUUZBackend(TranscriptionBackend):
    """Vosk Uzbek + Russian-only faster-whisper, selected per speaker turn."""

    def __init__(self, config, *, russian_backend=None, uzbek_backend=None):
        self.config = config
        self.russian = russian_backend or FasterWhisperBackend(config, language="ru")
        self.uzbek = uzbek_backend or VoskUzbekBackend(config)

    def transcribe(self, audio_path, *, start, end, initial_prompt):
        # Sequential execution keeps peak transient memory lower than parallel inference.
        uzbek = self.uzbek.transcribe(
            audio_path, start=start, end=end, initial_prompt=initial_prompt,
        )
        russian = self.russian.transcribe(
            audio_path, start=start, end=end, initial_prompt=initial_prompt,
        )
        return choose_ru_uz_by_time(russian, uzbek)
