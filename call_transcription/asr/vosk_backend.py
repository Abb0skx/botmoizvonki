from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from .base import TranscriptionBackend, inference_error
from ..audio import read_samples
from ..errors import ConfigurationError
from ..models import ASRSegment, Word


class VoskUzbekBackend(TranscriptionBackend):
    """Offline Uzbek recognizer using a caller-provisioned local Vosk model."""

    def __init__(self, config):
        self.config = config
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return
        try:
            import vosk
        except ImportError as exc:
            raise ConfigurationError("Установите vosk из requirements-transcription-linux.txt") from exc
        vosk.SetLogLevel(-1)
        self._model = vosk.Model(str(Path(self.config.vosk_model_path).resolve()))

    @staticmethod
    def _segment(payload, duration):
        text = str(payload.get("text") or "").strip()
        items = payload.get("result") or []
        words = [
            Word(float(item["start"]), float(item["end"]), str(item["word"]), float(item["conf"]))
            for item in items
            if item.get("word") and "start" in item and "end" in item and "conf" in item
        ]
        if not text:
            return None
        confidence = sum(word.confidence for word in words if word.confidence is not None) / len(words) if words else None
        start = words[0].start if words else 0.0
        end = words[-1].end if words else duration
        safe_confidence = max(1e-6, min(1.0, confidence if confidence is not None else 0.5))
        return ASRSegment(
            start, end, text, words, confidence,
            no_speech_probability=0.0,
            avg_logprob=math.log(safe_confidence),
            compression_ratio=0.0,
            language="uz",
        )

    def transcribe(self, audio_path, *, start, end, initial_prompt):
        del initial_prompt  # Vosk small models do not accept a free-form prompt.
        with self._lock:
            try:
                self._load()
                import numpy as np
                from vosk import KaldiRecognizer

                samples = read_samples(audio_path, start, end)
                pcm = np.clip(samples * 32768, -32768, 32767).astype("<i2").tobytes()
                recognizer = KaldiRecognizer(self._model, 16000)
                recognizer.SetWords(True)
                payloads = []
                # Short feeds let Vosk finalize natural pauses inside a turn.
                for offset in range(0, len(pcm), 8000):
                    if recognizer.AcceptWaveform(pcm[offset:offset + 8000]):
                        payloads.append(json.loads(recognizer.Result()))
                payloads.append(json.loads(recognizer.FinalResult()))
                duration = max(0.0, end - start)
                return [segment for payload in payloads if (segment := self._segment(payload, duration))]
            except ConfigurationError:
                raise
            except Exception as exc:
                raise inference_error(exc) from exc
