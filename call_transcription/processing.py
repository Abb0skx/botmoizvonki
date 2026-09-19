from __future__ import annotations

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
