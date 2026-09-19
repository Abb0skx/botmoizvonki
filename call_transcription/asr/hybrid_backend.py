from __future__ import annotations

from .base import TranscriptionBackend
from .faster_whisper_backend import FasterWhisperBackend
from .vosk_backend import VoskUzbekBackend
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


class HybridRUUZBackend(TranscriptionBackend):
    """Vosk Uzbek + Russian-only faster-whisper, selected per speaker turn."""

    requires_speaker_chunks = True

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
        selected = choose_ru_uz(russian, uzbek)
        language = "uz" if selected is uzbek else "ru"
        for segment in selected:
            segment.language = language
        return selected
