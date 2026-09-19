from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


@dataclass(frozen=True)
class TranscriptionConfig:
    backend: str = "auto"
    whisper_model: str = "large-v3-turbo"
    model_path: str = "models/whisper"
    diarization_model_path: str = "models/diarization"
    vosk_model_path: str = "models/vosk-uz"
    device: str = "auto"
    compute_type: str = "auto"
    num_speakers: int = 2
    languages: tuple[str, ...] = ("ru", "uz")
    use_vad: bool = True
    use_diarization: bool = True
    identify_roles: bool = True
    merge_gap_seconds: float = 0.8
    normalize_audio: bool = True
    max_duration_seconds: float = 1800
    max_bytes: int = 100 * 1024 * 1024
    chunk_seconds: float = 20
    speech_gap_seconds: float = 0.8
    cpu_threads: int = 2
    diarization_batch_size: int = 32
    diarization_max_duration_seconds: float = 180
    role_confidence_threshold: float = 0.8
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    # Never delete a caller-owned input. By default, no additional original is retained.
    archive_original_dir: str | None = None

    def __post_init__(self):
        if self.whisper_model not in {"small", "medium", "large-v3", "large-v3-turbo"}:
            raise ConfigurationError("whisper_model: small, medium, large-v3 или large-v3-turbo")
        if self.backend not in {"auto", "mlx", "faster-whisper", "hybrid"}:
            raise ConfigurationError("backend: auto, mlx, faster-whisper или hybrid")
        if self.device not in {"auto", "cpu", "cuda"}:
            raise ConfigurationError("device: auto, cpu или cuda")
        if self.languages != ("ru", "uz"):
            raise ConfigurationError("Поддерживаются только languages=('ru', 'uz')")
        if not 1 <= self.num_speakers <= 8 or not 0 <= self.merge_gap_seconds <= 5:
            raise ConfigurationError("Проверьте num_speakers (1–8) и merge_gap_seconds (0–5)")
        if not 1 <= self.chunk_seconds <= 30 or self.max_duration_seconds <= 0:
            raise ConfigurationError("chunk_seconds: 1–30; max_duration_seconds > 0")
        if not 0 <= self.speech_gap_seconds <= 3:
            raise ConfigurationError("speech_gap_seconds: 0–3")
        if self.cpu_threads < 1 or self.max_bytes < 1 or not 0 <= self.role_confidence_threshold <= 1:
            raise ConfigurationError("Некорректные лимиты локального распознавания")
        if not 1 <= self.diarization_batch_size <= 64:
            raise ConfigurationError("diarization_batch_size: 1–64")
        if self.diarization_max_duration_seconds <= 0:
            raise ConfigurationError("diarization_max_duration_seconds должен быть больше 0")

    @classmethod
    def from_env(cls):
        def flag(name, default):
            return os.getenv(name, str(default)).lower() in {"true", "1", "yes", "on"}
        try:
            return cls(
                backend=os.getenv("LOCAL_TRANSCRIPTION_BACKEND", "auto"),
                whisper_model=os.getenv("LOCAL_WHISPER_MODEL", "large-v3-turbo"),
                model_path=os.getenv("LOCAL_WHISPER_MODEL_PATH", "models/whisper"),
                diarization_model_path=os.getenv("LOCAL_DIARIZATION_MODEL_PATH", "models/diarization"),
                vosk_model_path=os.getenv("LOCAL_VOSK_MODEL_PATH", "models/vosk-uz"),
                device=os.getenv("LOCAL_TRANSCRIPTION_DEVICE", "auto"),
                compute_type=os.getenv("LOCAL_TRANSCRIPTION_COMPUTE_TYPE", "auto"),
                num_speakers=int(os.getenv("LOCAL_TRANSCRIPTION_NUM_SPEAKERS", "2")),
                merge_gap_seconds=float(os.getenv("LOCAL_TRANSCRIPTION_MERGE_GAP", "0.8")),
                cpu_threads=int(os.getenv("LOCAL_TRANSCRIPTION_CPU_THREADS", "2")),
                diarization_batch_size=int(os.getenv("LOCAL_DIARIZATION_BATCH_SIZE", "32")),
                diarization_max_duration_seconds=float(os.getenv("LOCAL_DIARIZATION_MAX_DURATION_SECONDS", "180")),
                speech_gap_seconds=float(os.getenv("LOCAL_TRANSCRIPTION_SPEECH_GAP", "0.8")),
                max_duration_seconds=float(os.getenv("TRANSCRIPTION_MAX_DURATION_SECONDS", "1800")),
                max_bytes=int(os.getenv("TRANSCRIPTION_MAX_BYTES", str(100 * 1024 * 1024))),
                identify_roles=flag("LOCAL_TRANSCRIPTION_IDENTIFY_ROLES", True),
                archive_original_dir=os.getenv("LOCAL_TRANSCRIPTION_ARCHIVE_DIR") or None,
            )
        except ValueError as exc:
            raise ConfigurationError("Некорректное числовое значение LOCAL_TRANSCRIPTION_* / TRANSCRIPTION_*") from exc

    def resolved_backend(self):
        if self.backend != "auto":
            return self.backend
        return "mlx" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster-whisper"

    def check_model_paths(self):
        path = Path(self.model_path)
        if not path.is_dir() or not (path / "config.json").is_file():
            raise ConfigurationError("Нет локальной модели Whisper: задайте LOCAL_WHISPER_MODEL_PATH")
        if self.resolved_backend() in {"faster-whisper", "hybrid"} and not (path / "model.bin").is_file():
            raise ConfigurationError("В LOCAL_WHISPER_MODEL_PATH нет CTranslate2 model.bin")
        if self.resolved_backend() == "mlx" and not (list(path.glob("*.safetensors")) or list(path.glob("*.npz"))):
            raise ConfigurationError("В LOCAL_WHISPER_MODEL_PATH нет MLX весов")
        if self.resolved_backend() == "hybrid":
            vosk_path = Path(self.vosk_model_path)
            if not vosk_path.is_dir() or not (vosk_path / "conf" / "model.conf").is_file():
                raise ConfigurationError("Нет локальной модели Vosk Uzbek: задайте LOCAL_VOSK_MODEL_PATH")
        if (self.use_diarization or self.use_vad) and not Path(self.diarization_model_path, "config.yaml").is_file():
            raise ConfigurationError("Нет локальной модели pyannote: задайте LOCAL_DIARIZATION_MODEL_PATH")


def enforce_offline():
    """No implicit model downloads or telemetry in the inference process."""
    for name, value in {
        "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "PYANNOTE_METRICS_ENABLED": "0",
        "DO_NOT_TRACK": "1",
    }.items():
        os.environ[name] = value
    # huggingface_hub may have been imported by a different local module already.
    import sys
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        constants.HF_HUB_OFFLINE = True
        constants.HF_HUB_DISABLE_TELEMETRY = True
