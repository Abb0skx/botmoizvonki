import math
import threading
from pathlib import Path

from .base import TranscriptionBackend, inference_error
from ..audio import read_samples
from ..config import enforce_offline
from ..errors import ConfigurationError
from ..models import ASRSegment, Word


# mlx-whisper caches one ModelHolder per process. Serialize all users of it.
_MLX_LOCK = threading.Lock()


class MLXWhisperBackend(TranscriptionBackend):
    def __init__(self, config):
        self.config = config

    def transcribe(self, audio_path, *, start, end, initial_prompt):
        enforce_offline()
        with _MLX_LOCK:
            try:
                import mlx_whisper
                result = mlx_whisper.transcribe(
                    read_samples(audio_path, start, end),
                    path_or_hf_repo=str(Path(self.config.model_path).resolve()),
                    task="transcribe", language=None, initial_prompt=initial_prompt,
                    word_timestamps=True, condition_on_previous_text=False,
                    temperature=0, no_speech_threshold=0.6, verbose=None,
                    hallucination_silence_threshold=2,
                )
                return [ASRSegment(
                    s["start"], s["end"], s["text"],
                    [Word(w["start"], w["end"], w["word"], w.get("probability")) for w in s.get("words", [])],
                    math.exp(min(0, s.get("avg_logprob", 0))), s.get("no_speech_prob", 0),
                    s.get("avg_logprob", 0), s.get("compression_ratio", 0),
                ) for s in result.get("segments", [])]
            except ImportError as exc:
                raise ConfigurationError("На Apple Silicon установите requirements-transcription-mac.txt") from exc
            except Exception as exc:
                raise inference_error(exc) from exc
