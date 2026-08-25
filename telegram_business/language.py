from __future__ import annotations

import re

# Product names ("iPhone 16 Pro Max", "Samsung S25") intentionally do not
# appear here: a model name alone must not select a language.
UZ_LATIN = {
    "assalomu", "salom", "narx", "narxi", "narxlar", "qancha", "necha",
    "kerak", "bormi", "olaman", "yuboring", "rang", "xotira", "rahmat",
    "lokatsiya", "manzil", "kredit", "nasiya", "uchun", "iltimos", "qachon",
    "yetkazib", "bering", "menejer", "topilmadi", "tulov", "to'lov",
}
RU = {
    "здравствуйте", "привет", "цена", "цены", "сколько", "нужен", "нужна",
    "модель", "есть", "беру", "отправьте", "цвет", "память", "спасибо",
    "доставка", "кредит", "рассрочка", "пожалуйста", "адрес", "менеджер", "когда",
    "оплата", "наличие",
}
UZ_CYRILLIC_WORDS = {
    "ассалому", "салом", "нарх", "нархи", "қанча", "керак", "борми",
    "оламан", "юборинг", "ранг", "хотира", "раҳмат", "манзил", "учун", "илтимос",
    "менежер", "кредит", "насия", "етказиб", "тўлов",
}
UZ_CYRILLIC = re.compile(r"[ўқғҳ]")
CYRILLIC = re.compile(r"[а-яё]")


def _scores(text: str) -> tuple[int, int]:
    lowered = str(text or "").casefold().replace("’", "'").replace("‘", "'").replace("ʻ", "'")
    words = set(re.findall(r"[\w']+", lowered))
    uz = len(words & UZ_LATIN) * 2 + len(words & UZ_CYRILLIC_WORDS) * 2
    ru = len(words & RU) * 2
    # Uzbek-specific Cyrillic characters are strong evidence. Generic Cyrillic
    # is only weak evidence because Uzbek Cyrillic may contain none of them.
    if UZ_CYRILLIC.search(lowered):
        uz += 4
    elif CYRILLIC.search(lowered) and ru == 0 and uz == 0:
        ru += 1
    return ru, uz


def detect_language(
    text: str,
    saved: str | None = None,
    telegram_code: str | None = None,
    history: str | None = None,
) -> tuple[str, float]:
    """Deterministically apply current -> saved -> history -> Telegram order.

    Strong evidence in the current message always wins, which lets a client
    explicitly switch language. Weak/no evidence (most notably a bare model
    name) falls through to conversation context.
    """
    ru, uz = _scores(text)
    if max(ru, uz) >= 2 and ru != uz:
        score = max(ru, uz)
        return ("ru" if ru > uz else "uz"), min(1.0, 0.58 + score * 0.055)
    if saved in {"ru", "uz"}:
        return saved, 0.48
    history_ru, history_uz = _scores(history or "")
    if max(history_ru, history_uz) >= 2 and history_ru != history_uz:
        return ("ru" if history_ru > history_uz else "uz"), 0.42
    code = str(telegram_code or "").lower()
    if code.startswith("uz"):
        return "uz", 0.35
    if code.startswith("ru"):
        return "ru", 0.35
    return "bi", 0.0
