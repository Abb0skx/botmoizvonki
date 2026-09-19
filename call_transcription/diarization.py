from __future__ import annotations

import math
import threading
from dataclasses import replace
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
        self.overlap_intervals = []

    def diarize(self, path):
        enforce_offline()
        with self._lock:
            try:
                import torch
                from pyannote.audio import Pipeline
                if self._pipeline is None:
                    torch.set_num_threads(self.config.cpu_threads)
                    pipeline = Pipeline.from_pretrained(str(Path(self.config.diarization_model_path).resolve()))
                    # Smaller batches reduce intermediate tensor memory on a VPS,
                    # without replacing the models or changing speaker thresholds.
                    pipeline.segmentation_batch_size = self.config.diarization_batch_size
                    pipeline.embedding_batch_size = self.config.diarization_batch_size
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
                regular = [SpeakerTurn(str(speaker), float(turn.start), float(turn.end))
                           for turn, _, speaker in output.speaker_diarization.itertracks(yield_label=True)]
                self.overlap_intervals = []
                for index, first in enumerate(regular):
                    for second in regular[index + 1:]:
                        if second.start >= first.end:
                            break
                        if first.speaker != second.speaker:
                            a, b = max(first.start, second.start), min(first.end, second.end)
                            if b > a:
                                self.overlap_intervals.append((a, b))
                annotation = output.speaker_diarization
                if self.config.exclusive_diarization:
                    exclusive = getattr(output, "exclusive_speaker_diarization", None)
                    if exclusive is not None:
                        annotation = exclusive
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


def speech_chunks(turns, max_seconds, max_gap=0.8):
    """Union nearby speech into bounded ASR windows, excluding long silences.

    Speaker changes are NOT ASR boundaries: short isolated words lose context
    and repeat Whisper's encoder work. Word timestamps are still aligned against
    all original turns afterwards, including uncertain simultaneous speech.
    """
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be positive and finite")
    if not math.isfinite(max_gap) or max_gap < 0:
        raise ValueError("max_gap must be non-negative and finite")
    windows = []
    for turn in sorted(turns, key=lambda t: (t.start, t.end)):
        if not math.isfinite(turn.start) or not math.isfinite(turn.end) or turn.end <= turn.start:
            continue
        if windows and turn.start <= windows[-1][1] + max_gap:
            windows[-1] = (windows[-1][0], max(windows[-1][1], turn.end))
        else:
            windows.append((turn.start, turn.end))
    for start, end in windows:
        while start < end:
            stop = min(end, start + max_seconds)
            yield start, stop
            start = stop


def speaker_speech_chunks(turns, max_seconds, max_gap=0.35):
    """Short speaker-homogeneous windows for per-utterance language routing.

    Consecutive turns from different speakers are never joined. This costs more
    ASR calls than ``speech_chunks`` but prevents one language decision from
    being applied to both sides of a bilingual conversation.
    """
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be positive and finite")
    windows = []
    for turn in sorted(turns, key=lambda item: (item.start, item.end, item.speaker)):
        if not math.isfinite(turn.start) or not math.isfinite(turn.end) or turn.end <= turn.start:
            continue
        if (windows and windows[-1][2] == turn.speaker
                and turn.start <= windows[-1][1] + max_gap):
            windows[-1] = (windows[-1][0], max(windows[-1][1], turn.end), turn.speaker)
        else:
            windows.append((turn.start, turn.end, turn.speaker))
    for window_start, window_end, _ in windows:
        start = window_start
        while start < window_end:
            stop = min(window_end, start + max_seconds)
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
        result.append(TranscriptSegment(
            start, end, speaker, None, text, language=segment.language or "unknown",
            confidence=confidence, overlap=overlap,
        ))
    return result


def resolve_two_speaker_unknowns(segments, turns):
    """Assign ambiguous words when the call is known to contain two people.

    ``assign_speaker`` deliberately marks comparable overlap as unknown. For a
    two-party phone call that uncertainty should remain in ``overlap=True``, but
    it should not create a fictitious third participant. Prefer the nearest
    known transcript neighbour, then diarization overlap/distance. The actual
    two pyannote speaker IDs are never renamed or merged.
    """
    speaker_ids = sorted({turn.speaker for turn in turns})
    if len(speaker_ids) != 2:
        return list(segments)

    resolved = [replace(segment) for segment in segments]

    def neighbour(index, step):
        cursor = index + step
        while 0 <= cursor < len(resolved):
            if resolved[cursor].speaker_id in speaker_ids:
                return resolved[cursor]
            cursor += step
        return None

    for index, segment in enumerate(resolved):
        if segment.speaker_id != "SPEAKER_UNKNOWN":
            continue

        previous = neighbour(index, -1)
        following = neighbour(index, 1)
        candidates = set(speaker_ids)

        scores = {speaker: 0.0 for speaker in speaker_ids}
        for turn in turns:
            overlap = max(
                0.0,
                min(segment.end, turn.end) - max(segment.start, turn.start),
            )
            scores[turn.speaker] += overlap
        best_score = max(scores.values())
        if best_score > 0:
            candidates = {
                speaker
                for speaker, score in scores.items()
                if math.isclose(score, best_score, rel_tol=0.05, abs_tol=0.02)
            }

        chosen = None
        if previous and following and previous.speaker_id == following.speaker_id:
            if previous.speaker_id in candidates:
                chosen = previous.speaker_id
        elif previous or following:
            previous_gap = (
                max(0.0, segment.start - previous.end)
                if previous and previous.speaker_id in candidates
                else math.inf
            )
            following_gap = (
                max(0.0, following.start - segment.end)
                if following and following.speaker_id in candidates
                else math.inf
            )
            if previous_gap <= following_gap and previous_gap < math.inf:
                chosen = previous.speaker_id
            elif following_gap < math.inf:
                chosen = following.speaker_id

        if chosen is None:
            def speaker_distance(speaker):
                return min(
                    max(turn.start - segment.end, segment.start - turn.end, 0.0)
                    for turn in turns
                    if turn.speaker == speaker
                )

            chosen = min(candidates, key=lambda speaker: (speaker_distance(speaker), speaker))

        resolved[index] = replace(segment, speaker_id=chosen, overlap=True)

    return resolved
