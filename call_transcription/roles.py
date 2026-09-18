from dataclasses import dataclass, asdict
import re
from typing import Protocol

from .processing import normalized_words


@dataclass(frozen=True)
class RoleResolution:
    manager_speaker: str | None = None
    confidence: float = 0
    method: str = "unresolved"

    def to_dict(self):
        return asdict(self)


class VoiceprintMatcher(Protocol):
    """Future extension: compare consented local embeddings; no enrolment in MVP."""
    def resolve(self, audio_path, turns) -> RoleResolution: ...


class RoleResolver:
    # Greetings, 'bor', 'цена', and 'narxi' alone occur on BOTH sides.
    # Only contextual service phrases give substantial evidence.
    _CUES = {
        "магазин texnikach": 5, "texnikach слушаю": 5, "слушаю вас": 4,
        "чем могу помочь": 4, "есть в наличии": 3, "сейчас посмотрю": 3,
        "можем доставить": 3, "оставьте номер": 3,
        "qanday yordam beraman": 4, "yetkazib beramiz": 3, "hozir tekshiraman": 3,
        "texnikach do'kon": 5, "texnikach eshitaman": 5,
    }

    def resolve(self, segments, *, threshold=0.8):
        ids = list(dict.fromkeys(s.speaker_id for s in segments if s.speaker_id != "SPEAKER_UNKNOWN"))
        if len(ids) != 2:
            return RoleResolution()
        scores = {speaker: 0.0 for speaker in ids}
        seen = set()
        for index, segment in enumerate(segments):
            if segment.speaker_id not in scores or segment.overlap:
                continue
            text = " ".join(normalized_words(segment.text))
            for cue, weight in self._CUES.items():
                key = segment.speaker_id, cue
                if key not in seen and re.search(r"(?<!\w)" + re.escape(cue) + r"(?!\w)", text):
                    scores[segment.speaker_id] += weight * (1 if index < 8 else 0.6)
                    seen.add(key)
        ranked = sorted(scores, key=scores.get, reverse=True)
        high, low = scores[ranked[0]], scores[ranked[1]]
        if high < 4 or high - low < 3:
            return RoleResolution(confidence=0.4 if high else 0, method="heuristic")
        confidence = min(0.95, 0.55 + min(high, 10) * 0.04) * (1 - low / (high + 1))
        return RoleResolution(ranked[0] if confidence >= threshold else None, round(confidence, 3), "heuristic")
