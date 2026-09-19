from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class TranscriptSegment:
    start: float
    end: float
    speaker_id: str
    role: str | None
    text: str
    language: str | None = "unknown"
    confidence: float | None = None
    overlap: bool = False


@dataclass(frozen=True)
class SpeakerTurn:
    speaker: str
    start: float
    end: float


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    confidence: float | None = None


@dataclass
class ASRSegment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    confidence: float | None = None
    no_speech_probability: float = 0
    avg_logprob: float = 0
    compression_ratio: float = 0
    language: str | None = None


def timestamp(seconds):
    value = max(0, int(seconds))
    return f"{value // 3600:02}:{value // 60 % 60:02}:{value % 60:02}"


def private_write(path, text):
    target = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)


@dataclass
class CallTranscript:
    audio_path: str
    duration: float
    segments: list[TranscriptSegment]
    full_text: str
    speakers: dict
    backend: str = ""
    model: str = ""
    processing_seconds: float = 0
    role_resolution: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def dialogue(self):
        return [{"role": s.role or self.speakers[s.speaker_id]["label"], "text": s.text} for s in self.segments]

    def speaker_label(self, segment):
        return {"manager": "Менеджер", "client": "Клиент"}.get(
            segment.role, self.speakers[segment.speaker_id].get("label", segment.speaker_id)
        )

    def to_dict(self):
        return {**asdict(self), "dialogue": self.dialogue}

    def to_txt(self):
        return "\n".join(
            f"[{timestamp(s.start)}] {self.speaker_label(s)}:\n{s.text}" for s in self.segments
        ) + "\n"

    def save_json(self, path):
        private_write(path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n")

    def save_txt(self, path):
        private_write(path, self.to_txt())
