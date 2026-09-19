import math
import threading
from pathlib import Path

from .base import TranscriptionBackend, inference_error
from ..audio import read_samples
from ..config import enforce_offline
from ..errors import ConfigurationError
from ..models import ASRSegment, Word


class FasterWhisperBackend(TranscriptionBackend):
    def __init__(self, config, *, language=None, model_path=None):
        self.config = config
        self.language = language
        self.model_path = model_path or config.model_path
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
                        str(Path(self.model_path).resolve()), device=device,
                        compute_type=compute, cpu_threads=self.config.cpu_threads,
                        local_files_only=True, num_workers=1,
                    )
                samples = read_samples(audio_path, start, end)
                language = self.language
                if language is None:
                    _, _, language_probs = self._model.detect_language(audio=samples)
                    probabilities = dict(language_probs)
                    # Whisper's unrestricted detector frequently confuses Uzbek
                    # telephone speech with Turkish/Azerbaijani and may even emit
                    # CJK. Pick only between the two languages this business uses.
                    language = max(
                        self.config.languages,
                        key=lambda code: probabilities.get(code, 0.0),
                    )
                segments, _ = self._model.transcribe(
                    samples, task="transcribe", language=language,
                    multilingual=False, initial_prompt=initial_prompt, word_timestamps=True,
                    condition_on_previous_text=False, vad_filter=self.config.use_vad,
                    vad_parameters={"min_silence_duration_ms": 300, "min_speech_duration_ms": 100},
                    temperature=0, beam_size=5, no_speech_threshold=0.6,
                )
                return [ASRSegment(
                    float(s.start), float(s.end), s.text,
                    [Word(float(w.start), float(w.end), w.word, float(w.probability)) for w in (s.words or [])],
                    math.exp(min(0, s.avg_logprob)), float(s.no_speech_prob), float(s.avg_logprob), float(s.compression_ratio),
                    language,
                ) for s in segments]
            except ImportError as exc:
                raise ConfigurationError("Установите requirements-transcription-linux.txt") from exc
            except Exception as exc:
                raise inference_error(exc) from exc
