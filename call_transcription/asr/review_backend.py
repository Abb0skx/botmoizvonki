"""Bounded local second pass with more context, preserving its evidence."""
from dataclasses import asdict, replace

from .base import TranscriptionBackend
from .hybrid_backend import _slice_segments
from ..processing import suspicious_segment, transcription_artifact


def reliability(segments):
    if not segments:
        return -2.0
    bad = sum(suspicious_segment(s) or transcription_artifact(s.text) for s in segments)
    scores = [s.confidence for s in segments if s.confidence is not None]
    return (sum(scores) / len(scores) if scores else 0.65) - bad


class ReviewBackend(TranscriptionBackend):
    def __init__(self, backend, max_reviews=2):
        self.backend = backend
        self.max_reviews = max_reviews
        self.reviews = []
        self.duration = 0

    @property
    def requires_speaker_chunks(self):
        return getattr(self.backend, "requires_speaker_chunks", False)

    def begin_call(self, duration):
        self.duration = duration
        self.reviews = []

    def transcribe(self, audio_path, *, start, end, initial_prompt):
        original = self.backend.transcribe(audio_path, start=start, end=end, initial_prompt=initial_prompt)
        if reliability(original) >= 0.35 or len(self.reviews) >= self.max_reviews:
            return original
        expanded_start, expanded_end = max(0, start - 1.0), min(self.duration, end + 1.0)
        if (expanded_start, expanded_end) == (start, end):
            return original
        retry = self.backend.transcribe(audio_path, start=expanded_start, end=expanded_end, initial_prompt=initial_prompt)
        offset = start - expanded_start
        # Keep only words that belong to the original interval, so adjacent
        # windows never duplicate the extra context.
        trimmed = _slice_segments(retry, offset, end - expanded_start, None)
        adjusted = [replace(s, start=max(0, s.start - offset), end=min(end - start, s.end - offset),
                            words=[replace(w, start=max(0, w.start - offset), end=min(end - start, w.end - offset))
                                   for w in s.words]) for s in trimmed]
        use_retry = reliability(adjusted) > reliability(original) + 0.1
        self.reviews.append({"start": start, "end": end, "used_retry": use_retry,
                             "original": [asdict(s) for s in original],
                             "retry": [asdict(s) for s in adjusted]})
        return adjusted if use_retry else original
