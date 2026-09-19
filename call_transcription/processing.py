from __future__ import annotations

import copy
import math
import re
from dataclasses import replace

BRANDS = (
    "Apple", "iPhone", "Samsung", "Galaxy", "Xiaomi", "Redmi", "Poco", "Honor", "Huawei",
    "Google Pixel", "Tecno", "Infinix", "MacBook", "iPad", "AirPods", "Apple Watch", "Galaxy Watch",
    "PlayStation", "Xbox", "Dyson", "JBL", "Marshall", "Sony", "Anker", "Nothing", "CMF", "Amazfit",
)

_UZ = set("ha yo'q yoq bor bormi narx narxi qancha necha salom assalomu assalamu alaykum alaikum alo aka yaxshimisiz rahmat kerak rang qora oq xotira gigabayt million ming so'm som dollar telefon obyavleniya chexol sotyapsizlar sotmaysizlar yetkazib beramiz hozir tekshiraman eshitaman do'kon dukon uchun ham bilan bo'ladi boladi mumkin yo'qmi yoqmi aha yaxshi xop xo'p haqiqat bor ekan men biz siz olaman bermoq olib berish xizmat qaysi shu bu sizga qanday yordam beraman narxini ayting йўқ ҳа бор борми нархи қанча керак раҳмат салом эшитаман дўкон ҳозир етказиб берамиз".split())
_RU = set("да нет здравствуйте магазин слушаю вас чем могу помочь есть наличии сейчас посмотрю цена доставка можем доставить оставьте номер черный черный чёрный тоже сколько стоит нужен нужна какой память гигабайт рублей сум долларов пожалуйста спасибо хорошо кажется цвет это можно мне мы я вы вам у нас будет".split())
_FORBIDDEN_SCRIPT = re.compile(
    r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0600-\u06ff]"
)
_NON_RU_UZ_LATIN = re.compile(r"[ğĞşŞıİöÖüÜçÇəƏ]", flags=re.UNICODE)
_EXCESSIVE_WORD_REPEAT = re.compile(
    r"(?i)(?<!\w)([^\W\d_]+(?:['’‘ʻʼ`][^\W\d_]+)*)([.!?,;:]?)"
    r"(?:\s+\1[.!?,;:]?){3,}(?!\w)"
)
_PUNCTUATION_ONLY = re.compile(r"^[\s\W_]+$", flags=re.UNICODE)


def normalized_words(text):
    text = text.casefold().translate(str.maketrans({"‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", "`": "'", "ё": "е"}))
    return re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)*", text)


def detect_language(text, context_terms=()):
    """Conservative local RU/UZ lexicon, not a claim of calibrated detection."""
    for term in sorted((*BRANDS, *context_terms), key=len, reverse=True):
        text = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", " ", text, flags=re.I)
    words = set(normalized_words(text))
    uz = bool(words & _UZ or re.search("[ўқғҳ]", text.casefold()))
    ru = bool(words & _RU)
    if not uz and not ru:
        # Do not infer a language from a brand, a number, or an arbitrary Latin token.
        ru = len(re.findall(r"[а-яё]+", text.casefold())) >= 2
    return "mixed" if ru and uz else "ru" if ru else "uz" if uz else "unknown"


def language_evidence(text, context_terms=()):
    """Comparable lexical/script evidence used only to choose a local backend."""
    cleaned = text
    for term in sorted((*BRANDS, *context_terms), key=len, reverse=True):
        cleaned = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", " ", cleaned, flags=re.I)
    words = normalized_words(cleaned)
    lowered = cleaned.casefold()
    latin = [word for word in words if re.search(r"[a-z]", word)]
    cyrillic = [word for word in words if re.search(r"[а-яёўқғҳ]", word)]
    uz_hits = sum(word in _UZ for word in words)
    ru_hits = sum(word in _RU for word in words)
    uz_hits += len(re.findall(r"\b\w+(?:ning|dan|ga|da|lar|mi|chi|siz|miz)\b", lowered))
    if re.search(r"[ўқғҳ]", lowered):
        uz_hits += 2
    return {
        "uz": uz_hits,
        "ru": ru_hits,
        "latin": len(latin),
        "cyrillic": len(cyrillic),
        "words": len(words),
    }


def normalize_text(text):
    # No paraphrasing, capitalization guesses, numeral conversion or punctuation invention.
    text = re.sub(r"\s+", " ", text).strip()
    # Word timestamps can expose the Uzbek apostrophe as a separate token
    # ("so 'ramoqchi", "yo 'q"). Joining it inside a word preserves the
    # recognized characters and prevents diarization from adding fake spaces.
    text = re.sub(
        r"(?<=\w)\s*(['’‘ʻʼ`])\s*(?=\w)",
        lambda match: match.group(1),
        text,
    )
    # Whisper can repeat a short token many times on telephone noise. Four or
    # more identical consecutive words are an ASR artefact for this two-party
    # workflow. Preserve one occurrence; ordinary double/triple emphasis and
    # short real answers remain untouched.
    text = _EXCESSIVE_WORD_REPEAT.sub(
        lambda match: match.group(1) + (match.group(2) or ""),
        text,
    )
    return re.sub(r"\s+([,.!?;:])", r"\1", text)


def transcription_artifact(text):
    """Identify only narrow, high-confidence non-speech artefacts.

    Calls in this project are restricted to Russian and Uzbek. Whisper may emit
    the English greeting ``Hello`` repeatedly on telephone noise. Do not apply
    a general foreign-word filter because Latin-script Uzbek and product names
    are valid; discard only a segment made exclusively from that known token.
    """
    normalized = normalize_text(text)
    if not normalized or _PUNCTUATION_ONLY.fullmatch(normalized):
        return True
    words = normalized_words(normalized)
    return bool(words) and set(words) == {"hello"}


def suspicious_segment(segment, previous=None):
    short_answer = " ".join(normalized_words(segment.text)) in {"да", "нет", "bor", "yo'q", "yoq", "ha", "aha"}
    if _FORBIDDEN_SCRIPT.search(segment.text) or _NON_RU_UZ_LATIN.search(segment.text):
        return True
    if segment.no_speech_probability > 0.9 and segment.avg_logprob < -1.5:
        return True
    if not short_answer and segment.no_speech_probability > 0.6 and segment.avg_logprob < -1:
        return True
    if not short_answer and segment.compression_ratio > 3 and segment.avg_logprob < -1:
        return True
    return bool(
        previous and len(normalized_words(segment.text)) >= 4
        and normalize_text(segment.text).casefold() == normalize_text(previous.text).casefold()
        and segment.avg_logprob < -1
    )


def merge_same_speaker(segments, merge_gap=0.8):
    merged = []
    for segment in sorted(segments, key=lambda s: (s.start, s.end)):
        if (merged and merged[-1].speaker_id == segment.speaker_id
                and merged[-1].role == segment.role and merged[-1].overlap == segment.overlap
                and 0 <= segment.start - merged[-1].end <= merge_gap):
            previous = merged[-1]
            confidence = [c for c in (previous.confidence, segment.confidence) if c is not None]
            languages = {value for value in (previous.language, segment.language) if value not in {None, "unknown"}}
            language = next(iter(languages)) if len(languages) == 1 else "mixed" if languages else "unknown"
            merged[-1] = replace(
                previous, end=segment.end, text=normalize_text(previous.text + " " + segment.text),
                confidence=min(confidence) if confidence else None, language=language,
            )
        else:
            merged.append(replace(segment))
    return merged


def sanitize_transcript_dict(payload):
    """Repair display-only artefacts in an already stored transcript.

    Historical rows may contain ``SPEAKER_UNKNOWN`` from overlap alignment.
    With exactly two known participants, attach those fragments to the closest
    neighbouring real speaker and keep ``overlap=true`` as the uncertainty
    signal. This function never invents or rewrites lexical content beyond the
    conservative normalization used during live transcription.
    """
    from .models import TranscriptSegment

    result = copy.deepcopy(payload)
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        return result

    segments = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        try:
            segments.append(TranscriptSegment(
                start=float(item["start"]),
                end=float(item["end"]),
                speaker_id=str(item["speaker_id"]),
                role=item.get("role"),
                text=normalize_text(str(item.get("text") or "")),
                language=item.get("language") or "unknown",
                confidence=item.get("confidence"),
                overlap=bool(item.get("overlap", False)),
            ))
        except (KeyError, TypeError, ValueError):
            continue

    known = sorted({
        segment.speaker_id
        for segment in segments
        if segment.speaker_id != "SPEAKER_UNKNOWN"
    })
    resolved_unknowns = len(known) == 2
    if resolved_unknowns:
        for index, segment in enumerate(segments):
            if segment.speaker_id != "SPEAKER_UNKNOWN":
                continue
            previous = next(
                (
                    candidate
                    for candidate in reversed(segments[:index])
                    if candidate.speaker_id in known
                ),
                None,
            )
            following = next(
                (
                    candidate
                    for candidate in segments[index + 1:]
                    if candidate.speaker_id in known
                ),
                None,
            )
            if previous and following and previous.speaker_id == following.speaker_id:
                chosen = previous.speaker_id
            elif previous or following:
                previous_gap = (
                    max(0.0, segment.start - previous.end)
                    if previous
                    else math.inf
                )
                following_gap = (
                    max(0.0, following.start - segment.end)
                    if following
                    else math.inf
                )
                chosen = (
                    previous.speaker_id
                    if previous_gap <= following_gap
                    else following.speaker_id
                )
            else:
                chosen = known[0]
            speaker = (result.get("speakers") or {}).get(chosen, {})
            segments[index] = replace(
                segment,
                speaker_id=chosen,
                role=segment.role or speaker.get("role"),
                overlap=True,
            )

    segments = [
        segment
        for segment in merge_same_speaker(segments)
        if not transcription_artifact(segment.text)
    ]
    speakers = {
        key: value
        for key, value in (result.get("speakers") or {}).items()
        if key != "SPEAKER_UNKNOWN" or not resolved_unknowns
    }
    result["segments"] = [
        {
            "start": segment.start,
            "end": segment.end,
            "speaker_id": segment.speaker_id,
            "role": segment.role,
            "text": segment.text,
            "language": segment.language,
            "confidence": segment.confidence,
            "overlap": segment.overlap,
        }
        for segment in segments
    ]
    result["speakers"] = speakers
    result["full_text"] = "\n".join(segment.text for segment in segments)
    result["dialogue"] = [
        {
            "role": segment.role or speakers.get(segment.speaker_id, {}).get(
                "label",
                segment.speaker_id,
            ),
            "text": segment.text,
        }
        for segment in segments
    ]
    return result


def transcript_dict_to_txt(payload):
    from .models import timestamp

    speakers = payload.get("speakers") or {}
    lines = []
    for segment in payload.get("segments") or []:
        role = segment.get("role")
        label = {"manager": "Менеджер", "client": "Клиент"}.get(
            role,
            speakers.get(segment.get("speaker_id"), {}).get(
                "label",
                segment.get("speaker_id", "speaker"),
            ),
        )
        lines.append(
            f"[{timestamp(segment.get('start', 0))}] {label}:\n{segment.get('text', '')}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


class ProductNameNormalizer:
    """Optional exact aliases only. No fuzzy guesses or generated catalogue names."""
    def __init__(self, context_terms=(), aliases=None):
        self.aliases = {alias: target for alias, target in (aliases or {}).items() if target in set(context_terms)}

    def normalize(self, text):
        for alias, target in sorted(self.aliases.items(), key=lambda item: len(item[0]), reverse=True):
            text = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", lambda _: target, text, flags=re.I)
        return text


def build_prompt(context_terms):
    terms = list(dict.fromkeys((*context_terms, "TEXNIKACH", "OLX", "Instagram", "Telegram", *BRANDS)))
    vocabulary = ", ".join(normalize_text(str(t)) for t in terms[:60])[:1000]
    return (
        "Телефонный разговор клиента и менеджера магазина только на русском и узбекском языках. "
        "Распознавать только русскую или узбекскую речь. "
        "Не переводить речь. Сохранять русские слова на русском, узбекские на узбекском, "
        "включая смешанные фразы. Названия электроники, модели, цены, числа и валюты. "
        "Suhbat rus va o'zbek tillarida. Tarjima qilmang. " + vocabulary
    )
