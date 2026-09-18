from __future__ import annotations

import math
import threading
from pathlib import Path

from .audio import read_samples
from .config import enforce_offline
from .errors import ConfigurationError, DiarizationError, ModelMemoryError
from .models import SpeakerTurn, TranscriptSegment


class PyannoteDiarizer:
    """Community-1 only, loaded from disk. Never instantiate a hosted pipeline."""
    def __init__(self, config):
        self.config = config
        self._pipeline = None
        self._lock = threading.Lock()

    def diarize(self, path):
        enforce_offline()
        with self._lock:
            try:
                import torch
                from pyannote.audio import Pipeline
                if self._pipeline is None:
                    torch.set_num_threads(self.config.cpu_threads)
                    pipeline = Pipeline.from_pretrained(str(Path(self.config.diarization_model_path).resolve()))
                    device = self.config.device
                    if device == "auto":
                        device = "cuda" if torch.cuda.is_available() else "cpu"
                    pipeline.to(torch.device(device))
                    self._pipeline = pipeline
                samples = read_samples(path)
                output = self._pipeline(
                    {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": 16000},
                    num_speakers=self.config.num_speakers if self.config.use_diarization else 1,
                )
                # Keep overlapping turns: exclusive diarization would hide uncertainty.
                annotation = output.speaker_diarization
                return [SpeakerTurn(
                    str(speaker) if self.config.use_diarization else "SPEAKER_UNKNOWN",
                    float(turn.start), float(turn.end),
                ) for turn, _, speaker in annotation.itertracks(yield_label=True)]
            except ImportError as exc:
                raise ConfigurationError("Установите pyannote.audio из requirements-transcription-*.txt") from exc
            except Exception as exc:
                if isinstance(exc, MemoryError) or "out of memory" in str(exc).lower():
                    raise ModelMemoryError("Недостаточно памяти для diarization") from exc
                raise DiarizationError("Локальное разделение голосов не удалось; проверьте модель community-1") from exc


def valid_turns(turns, duration):
    result = []
    for turn in turns:
        if not math.isfinite(turn.start) or not math.isfinite(turn.end):
            continue
        start, end = max(0, turn.start), min(duration, turn.end)
        if end > start:
            result.append(SpeakerTurn(turn.speaker, start, end))
    return sorted(result, key=lambda t: (t.start, t.end, t.speaker))


def speech_chunks(turns, max_seconds):
    """Partition speech at speaker changes; never give long silences to Whisper.

    The partition also ensures overlapping voices are transcribed once rather
    than duplicating the same audio for each speaker.
    """
    boundaries = sorted({t.start for t in turns} | {t.end for t in turns})
    windows = []
    for start, end in zip(boundaries, boundaries[1:]):
        active = frozenset(t.speaker for t in turns if t.start < end and t.end > start)
        if not active:
            continue
        if windows and windows[-1][2] == active and start - windows[-1][1] <= 0.15:
            windows[-1] = (windows[-1][0], end, active)
        else:
            windows.append((start, end, active))
    for start, end, _ in windows:
        while start < end:
            stop = min(end, start + max_seconds)
            yield start, stop
            start = stop


def assign_speaker(start, end, turns):
    scores = {}
    for turn in turns:
        overlap = max(0, min(end, turn.end) - max(start, turn.start))
        if overlap:
            scores[turn.speaker] = scores.get(turn.speaker, 0) + overlap
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return "SPEAKER_UNKNOWN", False
    # A word with comparable coverage by two voices is genuinely ambiguous.
    ambiguous = len(ordered) > 1 and ordered[1][1] >= ordered[0][1] * 0.65
    return ("SPEAKER_UNKNOWN" if ambiguous else ordered[0][0]), ambiguous


def align_segment(segment, offset, chunk_end, turns):
    from .processing import normalize_text
    units = segment.words or [segment]
    result = []
    for unit in units:
        if not math.isfinite(unit.start) or not math.isfinite(unit.end):
            continue
        start, end = max(offset, offset + unit.start), min(chunk_end, offset + unit.end)
        if end <= start or not unit.text.strip():
            continue
        speaker, overlap = assign_speaker(start, end, turns)
        text = normalize_text(unit.text)
        confidence = unit.confidence
        result.append(TranscriptSegment(start, end, speaker, None, text, confidence=confidence, overlap=overlap))
    return result
