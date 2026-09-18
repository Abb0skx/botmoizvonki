import math
import threading
from pathlib import Path

from .base import TranscriptionBackend, inference_error
from ..audio import read_samples
from ..config import enforce_offline
from ..errors import ConfigurationError
from ..models import ASRSegment, Word


class FasterWhisperBackend(TranscriptionBackend):
    def __init__(self, config):
        self.config = config
        self._model = None
        self._lock = threading.Lock()

    def transcribe(self, audio_path, *, start, end, initial_prompt):
        with self._lock:
            enforce_offline()
            try:
                if self._model is None:
                    from faster_whisper import WhisperModel
                    import ctranslate2
                    device = self.config.device
                    if device == "auto":
                        device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
                    compute = self.config.compute_type
                    if compute == "auto":
                        compute = "float16" if device == "cuda" else "int8"
                    self._model = WhisperModel(
                        str(Path(self.config.model_path).resolve()), device=device,
                        compute_type=compute, cpu_threads=self.config.cpu_threads,
                        local_files_only=True, num_workers=1,
                    )
                segments, _ = self._model.transcribe(
                    read_samples(audio_path, start, end), task="transcribe", language=None,
                    multilingual=True, initial_prompt=initial_prompt, word_timestamps=True,
                    condition_on_previous_text=False, vad_filter=self.config.use_vad,
                    vad_parameters={"min_silence_duration_ms": 300, "min_speech_duration_ms": 100},
                    temperature=0, beam_size=5, no_speech_threshold=0.6,
                )
                return [ASRSegment(
                    float(s.start), float(s.end), s.text,
                    [Word(float(w.start), float(w.end), w.word, float(w.probability)) for w in (s.words or [])],
                    math.exp(min(0, s.avg_logprob)), float(s.no_speech_prob), float(s.avg_logprob), float(s.compression_ratio),
                ) for s in segments]
            except ImportError as exc:
                raise ConfigurationError("Установите requirements-transcription-linux.txt") from exc
            except Exception as exc:
                raise inference_error(exc) from exc
