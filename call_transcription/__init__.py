"""Local-only call transcription. Importing this package loads no ML models."""
from .config import TranscriptionConfig
from .models import CallTranscript, TranscriptSegment
from .transcriber import CallTranscriber

__all__ = ["CallTranscriber", "TranscriptionConfig", "CallTranscript", "TranscriptSegment"]
