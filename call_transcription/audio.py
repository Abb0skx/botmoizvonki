from __future__ import annotations

import math
import subprocess
import wave
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import AudioDecodeError, NoSpeechDetectedError


def normalize_audio(source, target, config):
    source = Path(source).resolve()
    if source == Path(target).resolve():
        raise AudioDecodeError("Нормализованный файл не должен заменять оригинал")
    if not source.is_file():
        raise FileNotFoundError("Аудиофайл отсутствует")
    if source.stat().st_size == 0:
        raise NoSpeechDetectedError("Аудиофайл пуст")
    if source.stat().st_size > config.max_bytes:
        raise AudioDecodeError("Аудиофайл превышает разрешённый размер")
    try:
        probe = subprocess.run(
            [config.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
             "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, check=True, timeout=30,
        )
        duration = float(probe.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            raise NoSpeechDetectedError("В записи нет аудиоданных")
        if duration > config.max_duration_seconds:
            raise AudioDecodeError("Запись длиннее разрешённого лимита")
        subprocess.run(
            [config.ffmpeg, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
             "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
             "-t", str(config.max_duration_seconds + 1), "-c:a", "pcm_s16le", "-f", "wav", str(target)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
        )
    except FileNotFoundError as exc:
        raise AudioDecodeError("Нужны установленные ffmpeg и ffprobe") from exc
    except (subprocess.SubprocessError, ValueError) as exc:
        raise AudioDecodeError("Не удалось декодировать запись: проверьте формат и целостность файла") from exc


@contextmanager
def prepared_audio(source, config):
    """Temporary PCM is removed on success AND on any inference failure."""
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError("Аудиофайл отсутствует")
    if source.stat().st_size > config.max_bytes:
        raise AudioDecodeError("Аудиофайл превышает разрешённый размер")
    with TemporaryDirectory(prefix="call-asr-") as directory:
        path = Path(directory) / "normalized.wav"
        if config.normalize_audio:
            normalize_audio(source, path, config)
        else:
            path = source
        try:
            with wave.open(str(path), "rb") as wav:
                if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) != (1, 16000, 2):
                    raise AudioDecodeError("Без нормализации нужен mono PCM16 WAV, 16000 Hz")
                duration = wav.getnframes() / 16000
                if duration <= 0:
                    raise NoSpeechDetectedError("Аудиозапись пуста")
                if duration > config.max_duration_seconds:
                    raise AudioDecodeError("Запись длиннее разрешённого лимита")
        except (wave.Error, EOFError) as exc:
            raise AudioDecodeError("Повреждённый WAV") from exc
        yield path, duration


def read_samples(path, start=0, end=None):
    import numpy as np
    with wave.open(str(path), "rb") as wav:
        first = min(wav.getnframes(), max(0, round(start * 16000)))
        last = wav.getnframes() if end is None else min(wav.getnframes(), round(end * 16000))
        wav.setpos(first)
        return np.frombuffer(wav.readframes(max(0, last - first)), dtype="<i2").astype(np.float32) / 32768
