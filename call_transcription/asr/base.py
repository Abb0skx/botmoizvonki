from abc import ABC, abstractmethod

from ..models import ASRSegment


class TranscriptionBackend(ABC):
    @abstractmethod
    def transcribe(self, audio_path, *, start: float, end: float, initial_prompt: str) -> list[ASRSegment]:
        """Return chunk-relative word/segment timestamps; never translate."""


def inference_error(exc):
    from ..errors import ModelMemoryError, TranscriptionError
    if isinstance(exc, MemoryError) or "out of memory" in str(exc).lower():
        return ModelMemoryError("Недостаточно памяти для локальной модели; нужен другой worker/device")
    return TranscriptionError("Локальное распознавание завершилось ошибкой; проверьте модель и зависимости")
