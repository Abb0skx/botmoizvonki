from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from dataclasses import asdict

from .audio import prepared_audio
from .config import TranscriptionConfig
from .diarization import (
    PyannoteDiarizer,
    align_segment,
    resolve_two_speaker_unknowns,
    speaker_speech_chunks,
    speech_chunks,
    valid_turns,
)
from .errors import NoSpeechDetectedError
from .models import CallTranscript, SpeakerTurn
from .processing import (
    build_prompt,
    detect_language,
    merge_same_speaker,
    suspicious_segment,
    transcription_artifact,
    ProductNameNormalizer,
)
from .roles import RoleResolver, RoleResolution

logger = logging.getLogger(__name__)


class CallTranscriber:
    """Reuse one instance per worker; models are lazy, persistent and serialized."""
    def __init__(self, config=None, *, backend=None, diarizer=None, role_resolver=None,
                 context_terms_provider=None, voiceprint_matcher=None):
        self.config = config or TranscriptionConfig.from_env()
        self.backend = backend
        self.diarizer = diarizer
        self.role_resolver = role_resolver or RoleResolver()
        self.context_terms_provider = context_terms_provider
        self.voiceprint_matcher = voiceprint_matcher
        self._injected = backend is not None and diarizer is not None
        self._lock = threading.Lock()
        self._catalog = ([], {})

    def _initialize(self):
        if not self._injected:
            self.config.check_model_paths()
        if self.backend is None:
            if self.config.resolved_backend() == "gigaam":
                from .asr.gigaam_backend import GigaAMBackend
                self.backend = GigaAMBackend(self.config)
            elif self.config.resolved_backend() == "hybrid":
                from .asr.hybrid_backend import HybridRUUZBackend
                self.backend = HybridRUUZBackend(self.config)
            elif self.config.resolved_backend() == "mlx":
                from .asr.mlx_backend import MLXWhisperBackend
                self.backend = MLXWhisperBackend(self.config)
            else:
                from .asr.faster_whisper_backend import FasterWhisperBackend
                self.backend = FasterWhisperBackend(self.config)
        if self.diarizer is None:
            self.diarizer = PyannoteDiarizer(self.config)
        if self.config.retry_uncertain:
            from .asr.review_backend import ReviewBackend
            if not isinstance(self.backend, ReviewBackend):
                self.backend = ReviewBackend(self.backend)

    def transcribe(self, audio_path, context_terms=None, *, call_id=None):
        started = time.monotonic()
        with self._lock:
            try:
                result = self._transcribe(audio_path, context_terms, call_id)
                result.processing_seconds = round(time.monotonic() - started, 3)
                logger.info(
                    "call_transcription call_id=%s duration=%.3f processing=%.3f backend=%s model=%s speakers=%d segments=%d role_confidence=%.3f",
                    call_id, result.duration, result.processing_seconds, result.backend, result.model,
                    len(set(result.speakers) - {"SPEAKER_UNKNOWN"}), len(result.segments), result.role_resolution["confidence"],
                )
                return result
            except Exception as exc:
                # Never log exception arguments, input paths, prompts, or client text.
                logger.warning("call_transcription_failed call_id=%s error=%s processing=%.3f",
                               call_id, type(exc).__name__, time.monotonic() - started)
                raise

    def _transcribe(self, audio_path, context_terms, call_id):
        config = self.config
        with prepared_audio(audio_path, config) as (path, duration):
            if duration < 0.1:
                raise NoSpeechDetectedError("Запись короче 0.1 секунды")
            self._initialize()
            from .asr.review_backend import ReviewBackend
            if isinstance(self.backend, ReviewBackend):
                self.backend.begin_call(duration)
            terms = list(context_terms or [])
            from .catalog import load_catalog, catalogue_aliases
            catalog_terms, aliases = load_catalog(config.catalog_path)
            if catalog_terms:
                self._catalog = catalog_terms, aliases
            elif self._catalog[0]:
                catalog_terms, aliases = self._catalog
            terms.extend(catalog_terms)
            if self.context_terms_provider:
                terms.extend(self.context_terms_provider(call_id) or [])
            product_normalizer = ProductNameNormalizer(terms, catalogue_aliases(terms, aliases))
            long_audio_without_diarization = (
                config.use_diarization
                and duration > config.diarization_max_duration_seconds
            )
            if long_audio_without_diarization:
                # Pyannote clustering becomes disproportionately expensive on
                # long CPU-only calls. Preserve all speech for ASR, but do not
                # invent speaker identities when diarization is deliberately
                # skipped.
                turns = [SpeakerTurn("SPEAKER_UNKNOWN", 0, duration)]
            elif config.use_diarization or config.use_vad:
                turns = valid_turns(self.diarizer.diarize(path), duration)
            else:
                turns = [SpeakerTurn("SPEAKER_UNKNOWN", 0, duration)]
            if not turns:
                raise NoSpeechDetectedError("В записи не обнаружена речь")
            warnings = []
            if long_audio_without_diarization:
                warnings.append("diarization_skipped_for_long_audio")
            speakers_found = {t.speaker for t in turns}
            if len(speakers_found) != config.num_speakers:
                warnings.append("speaker_count_mismatch")
            aligned, previous, raw_segments = [], None, []
            chunks = speaker_speech_chunks(
                turns, config.chunk_seconds,
            ) if getattr(self.backend, "requires_speaker_chunks", False) is True else speech_chunks(
                turns, config.chunk_seconds, config.speech_gap_seconds,
            )
            for start, end in chunks:
                for segment in self.backend.transcribe(path, start=start, end=end, initial_prompt=build_prompt(terms)):
                    raw_segments.append({**asdict(segment), "window_start": start, "window_end": end})
                    if suspicious_segment(segment, previous):
                        warnings.append("suspicious_asr_segment_filtered")
                        continue
                    aligned.extend(align_segment(segment, start, end, turns))
                    previous = segment
            if config.num_speakers == 2:
                aligned = resolve_two_speaker_unknowns(aligned, turns)
            # Preserve original simultaneous-speech evidence even when the
            # exclusive timeline assigns exactly one speaker to each word.
            overlap_intervals = getattr(self.diarizer, "overlap_intervals", [])
            if isinstance(overlap_intervals, list):
                for segment in aligned:
                    if any(min(segment.end, b) > max(segment.start, a) for a, b in overlap_intervals):
                        segment.overlap = True
            for segment in aligned:
                segment.raw_text = segment.text
                if segment.confidence is not None and segment.confidence < config.uncertainty_threshold:
                    segment.uncertain = True
                    segment.text = "[неразборчиво]"
                    warnings.append("low_confidence_speech")
            segments = [
                segment
                for segment in merge_same_speaker(aligned, config.merge_gap_seconds)
                if not transcription_artifact(segment.text)
            ]
            if not segments:
                raise NoSpeechDetectedError("Нет достоверно распознанной речи")
            for segment in segments:
                if not segment.uncertain:
                    segment.text = product_normalizer.normalize(segment.text)
                detected = detect_language(segment.text, terms)
                hinted = segment.language if segment.language in {"ru", "uz"} else None
                segment.language = (
                    "mixed" if hinted and detected in {"ru", "uz"} and detected != hinted
                    else hinted or detected
                )
            resolution = RoleResolution()
            if config.identify_roles and config.use_diarization:
                resolution = self.role_resolver.resolve(segments, threshold=config.role_confidence_threshold)
                if self.voiceprint_matcher:
                    match = self.voiceprint_matcher.resolve(path, turns)
                    recognized = {s.speaker_id for s in segments} - {"SPEAKER_UNKNOWN"}
                    if len(recognized) == 2 and match.manager_speaker in recognized and match.confidence >= 0.9:
                        resolution = match
            ids = list(dict.fromkeys(s.speaker_id for s in segments))
            known = [s for s in ids if s != "SPEAKER_UNKNOWN"]
            speakers = {}
            for index, speaker in enumerate(ids, 1):
                role = None
                if resolution.manager_speaker and len(known) == 2 and speaker in known:
                    role = "manager" if speaker == resolution.manager_speaker else "client"
                speakers[speaker] = {"role": role, "role_confidence": resolution.confidence if role else 0,
                                     "label": f"speaker_{index}" if speaker != "SPEAKER_UNKNOWN" else "speaker_unknown"}
            for segment in segments:
                segment.role = speakers[segment.speaker_id]["role"]
            if any(s.overlap for s in segments):
                warnings.append("overlapping_speech_not_separated")
            result = CallTranscript(
                str(audio_path), duration, segments, "\n".join(s.text for s in segments), speakers,
                backend=config.resolved_backend(),
                model=(
                    f"{('GigaAM-v3-rnnt' if config.russian_engine == 'gigaam' else config.whisper_model)}+{config.uzbek_whisper_model}"
                    if config.resolved_backend() == "hybrid"
                    else config.gigaam_model if config.resolved_backend() == "gigaam" else config.whisper_model
                ),
                role_resolution=resolution.to_dict(), warnings=sorted(set(warnings)),
                raw_segments=raw_segments,
                quality={"uncertain_segments": sum(s.uncertain for s in segments),
                         "catalog_terms": len(catalog_terms),
                         "confidence_calibrated": False,
                         "exclusive_diarization": config.exclusive_diarization},
            )
            if isinstance(self.backend, ReviewBackend):
                result.quality["reviews"] = self.backend.reviews
        if config.archive_original_dir:
            directory = Path(config.archive_original_dir)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / (uuid.uuid4().hex + Path(audio_path).suffix)
            # Explicit opt-in copy; never remove or overwrite the caller's original.
            with target.open("xb") as output, Path(audio_path).open("rb") as source:
                os.chmod(target, 0o600)
                shutil.copyfileobj(source, output)
        return result
