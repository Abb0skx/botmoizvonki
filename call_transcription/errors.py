class TranscriptionError(RuntimeError):
    """Safe, actionable error; messages must not contain client speech."""


class ConfigurationError(TranscriptionError):
    pass


class AudioDecodeError(TranscriptionError):
    pass


class NoSpeechDetectedError(TranscriptionError):
    pass


class DiarizationError(TranscriptionError):
    pass


class ModelMemoryError(TranscriptionError):
    pass
