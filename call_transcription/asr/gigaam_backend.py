"""Local GigaAM inference. Checkpoints are explicitly provisioned before use."""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import wave
import math

from .base import TranscriptionBackend, inference_error
from ..audio import read_samples
from ..config import enforce_offline
from ..errors import ConfigurationError
from ..models import ASRSegment, Word
from ..processing import detect_language


class GigaAMBackend(TranscriptionBackend):
    def __init__(self, config, *, model_name=None):
        self.config = config
        self.model_name = model_name or config.gigaam_model
        self._model = None
        self._lock = threading.Lock()
        self._token_scores = []

    def _capture_ctc_scores(self, module, inputs, logits):
        # Keep only small CPU scalars, never encoder tensors between calls.
        # These posterior scores are useful for review, not calibrated accuracy.
        probabilities = logits.detach().softmax(dim=-1)[0]
        scores, labels = probabilities.max(dim=-1)
        previous = None
        emitted = []
        blank = self._model.decoding.blank_id
        for index, (label, score) in enumerate(zip(labels.cpu().tolist(), scores.cpu().tolist())):
            if label != blank and label != previous:
                emitted.append((index / len(labels), float(score)))
            previous = label
        self._token_scores = emitted

    def transcribe(self, audio_path, *, start, end, initial_prompt=""):
        with self._lock:
            enforce_offline()
            try:
                import torch
                import gigaam
                if self._model is None:
                    checkpoint = Path(self.config.gigaam_model_path, self.model_name + ".ckpt")
                    if not checkpoint.is_file():
                        raise ConfigurationError("Сначала скачайте checkpoint GigaAM")
                    # These two revisions use character vocabularies, no tokenizer download.
                    if self.model_name not in {"v3_rnnt", "multilingual_ctc"}:
                        raise ConfigurationError("Неподдерживаемая offline-версия GigaAM")
                    torch.set_num_threads(self.config.cpu_threads)
                    device = self.config.device
                    if device == "auto":
                        device = "cuda" if torch.cuda.is_available() else "cpu"
                    self._model = gigaam.load_model(
                        self.model_name, download_root=str(checkpoint.parent),
                        device=device, fp16_encoder=device != "cpu", use_flash=False,
                    )
                    if self.model_name == "multilingual_ctc":
                        self._model.head.register_forward_hook(self._capture_ctc_scores)
                # GigaAM's public short-audio API requires a WAV <=25 seconds.
                # Split defensively if a caller supplies a larger window.
                output = []
                offset = start
                with TemporaryDirectory(prefix="gigaam-") as tmp:
                    path = Path(tmp, "chunk.wav")
                    while offset < end:
                        stop = min(end, offset + 24)
                        samples = read_samples(audio_path, offset, stop)
                        if len(samples) == 0:
                            break
                        with wave.open(str(path), "wb") as wav:
                            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                            wav.writeframes((samples * 32767).clip(-32768, 32767).astype("<i2").tobytes())
                        self._token_scores = []
                        result = self._model.transcribe(str(path), word_timestamps=True)
                        text = result.text.strip()
                        if text:
                            relative = offset - start
                            def word_score(word):
                                values = [p for fraction, p in self._token_scores
                                          if float(word.start) <= fraction * (len(samples) / 16000) <= float(word.end)]
                                return math.exp(sum(math.log(max(p, 1e-8)) for p in values) / len(values)) if values else None
                            words = [Word(
                                float(w.start) + relative, float(w.end) + relative,
                                w.text, word_score(w),
                            ) for w in (result.words or [])]
                            scores = [w.confidence for w in words if w.confidence is not None]
                            output.append(ASRSegment(
                                relative, stop - start, text, words,
                                confidence=sum(scores) / len(scores) if scores else None,
                                language=detect_language(text),
                            ))
                        offset = stop
                return output
            except ConfigurationError:
                raise
            except ImportError as exc:
                raise ConfigurationError("Установите зависимости GigaAM в ASR worker") from exc
            except Exception as exc:
                raise inference_error(exc) from exc
