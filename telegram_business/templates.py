from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from types import MappingProxyType
from typing import Any, Mapping

CATALOG_URL = "https://texnikach.uz/go"

# Public sheet codes are deliberately stable. The aliases preserve compatibility
# with the first BusinessService implementation.
TEMPLATE_ALIASES = {
    "handoff": "human_handoff",
    "order": "order_request",
    "media": "media_only",
}

ALLOWED_PLACEHOLDERS = frozenset(
    {
        "manager_time_phrase_ru",
        "manager_time_phrase_uz",
        "model",
        "models",
        "variants",
        "catalog_url",
        "attribute_label",
        "details",
        "price",
        "phone",
        "location",
        "fulfillment",
        "preferred_time",
        "missing_fields",
    }
)

TEMPLATES: dict[str, dict[str, str]] = {
    "greeting_model": {
        "ru": "Здравствуйте! Я авто-помощник TEXNIKACH. Менеджер ответит {manager_time_phrase_ru}.",
        "uz": "Assalomu alaykum! Men TEXNIKACH avto-yordamchisiman. Menejer {manager_time_phrase_uz} javob beradi.",
    },
    "greeting_no_model": {
        "ru": "Здравствуйте! Я авто-помощник TEXNIKACH. Ночью могу показать цены или собрать заявку. Цену и наличие подтвердит менеджер {manager_time_phrase_ru}.",
        "uz": "Assalomu alaykum! Men TEXNIKACH avto-yordamchisiman. Kechasi narxlarni ko‘rsataman yoki so‘rov yig‘aman. Narx va mavjudlikni menejer {manager_time_phrase_uz} tasdiqlaydi.",
    },
    "credit": {
        "ru": "Я автоматический помощник TEXNIKACH. К сожалению, у нас нет кредита и рассрочки. Оплата производится полностью после проверки товара.\n\nАктуальные модели и цены: {catalog_url}",
        "uz": "Men TEXNIKACH avtomatik yordamchisiman. Afsuski, bizda kredit va bo‘lib to‘lash yo‘q. To‘lov mahsulot tekshirilgandan keyin to‘liq amalga oshiriladi.\n\nAktual modellar va narxlar: {catalog_url}",
    },
    "product_result": {
        "ru": "{model}\n\n{variants}",
        "uz": "{model}\n\n{variants}",
    },
    "ambiguous": {
        "ru": "Выберите модель:\n\n{models}",
        "uz": "Modelni tanlang:\n\n{models}",
    },
    "human_handoff": {
        "ru": "Ваше сообщение сохранено и передано менеджеру. Я не буду принимать решение по этому вопросу. Менеджер ответит вам {manager_time_phrase_ru}.",
        "uz": "Xabaringiz saqlandi va menejerga topshirildi. Bu masala bo‘yicha men qaror qabul qilmayman. Menejer sizga {manager_time_phrase_uz} javob beradi.",
    },
    "order_request": {
        "ru": "Я автоматический помощник и не могу оформить или подтвердить заказ. Я сохраню ваши пожелания для менеджера. Если ещё не отправили точную модель, память, цвет, локацию и удобное время, можете отправить их сейчас.",
        "uz": "Men avtomatik yordamchiman va buyurtmani rasmiylashtira yoki tasdiqlay olmayman. Istaklaringizni menejer uchun saqlab qo‘yaman. Agar aniq model, xotira, rang, lokatsiya va qulay vaqtni hali yubormagan bo‘lsangiz, hozir yuborishingiz mumkin.",
    },
    "media_only": {
        "ru": "Для автоматического поиска напишите название модели текстом. Фото, файл или голосовое сообщение увидит менеджер {manager_time_phrase_ru}.",
        "uz": "Avtomatik qidiruv uchun model nomini matn ko‘rinishida yozing. Rasm, fayl yoki ovozli xabarni menejer {manager_time_phrase_uz} ko‘radi.",
    },
    "not_found_1": {
        "ru": "Не удалось точно определить модель. Напишите полное название, например: Samsung Galaxy S26 Ultra или iPhone 16 Pro Max.",
        "uz": "Modelni aniq topa olmadim. To‘liq nomini yozing, masalan: Samsung Galaxy S26 Ultra yoki iPhone 16 Pro Max.",
    },
    "not_found_2": {
        "ru": "Запрос сохранён. Менеджер поможет найти нужную модель {manager_time_phrase_ru}.",
        "uz": "So‘rovingiz saqlandi. Menejer kerakli modelni {manager_time_phrase_uz} topishga yordam beradi.",
    },
    "location_before_model": {
        "ru": "Локацию получили. Напишите, пожалуйста, название нужной модели. Менеджер проверит данные {manager_time_phrase_ru}.",
        "uz": "Lokatsiyani oldik. Kerakli model nomini yozing. Menejer ma’lumotlarni {manager_time_phrase_uz} tekshiradi.",
    },
    "final": {
        "ru": "Цена и наличие требуют подтверждения менеджера {manager_time_phrase_ru}. Бот не оформляет заказ и не резервирует товар.",
        "uz": "Narx va mavjudlikni menejer {manager_time_phrase_uz} tasdiqlaydi. Bot buyurtma rasmiylashtirmaydi va mahsulotni band qilmaydi.",
    },
    "product_source_unavailable": {
        "ru": "Сейчас не удалось получить актуальные цены из базы. Цену и наличие проверит менеджер {manager_time_phrase_ru}.",
        "uz": "Hozir bazadan aktual narxlarni olishning imkoni bo‘lmadi. Narx va mavjudligini menejer {manager_time_phrase_uz} tekshiradi.",
    },
    "data_added": {
        "ru": "Данные добавлены.",
        "uz": "Ma’lumotlar qo‘shildi.",
    },
    "catalog": {
        "ru": "Актуальные модели и цены: {catalog_url}",
        "uz": "Aktual modellar va narxlar: {catalog_url}",
    },
    "delivery": {
        "ru": "По Ташкенту доставка бесплатная и обычно занимает 2–3 часа после подтверждения заказа, цены и наличия менеджером. Время можно согласовать с менеджером. Для адреса за пределами Ташкента условия проверит менеджер {manager_time_phrase_ru}.",
        "uz": "Toshkent bo‘ylab yetkazib berish bepul va buyurtma, narx hamda mavjudlik menejer tomonidan tasdiqlangandan keyin odatda 2–3 soat davom etadi. Vaqtni menejer bilan kelishish mumkin. Toshkentdan tashqaridagi manzil uchun shartlarni menejer {manager_time_phrase_uz} tekshiradi.",
    },
    "pickup": {
        "ru": "Самовывоз возможен только после подтверждения наличия менеджером. Пожалуйста, не приезжайте без подтверждения. Менеджер ответит вам {manager_time_phrase_ru}.",
        "uz": "Do‘kondan olib ketish faqat menejer mavjudlikni tasdiqlaganidan keyin mumkin. Tasdiqsiz kelmang. Menejer sizga {manager_time_phrase_uz} javob beradi.",
    },
    "availability": {
        "ru": "Наличие подтвердит менеджер {manager_time_phrase_ru}. Напишите точное название модели, и я покажу цены из текущей базы.",
        "uz": "Mavjudligini menejer {manager_time_phrase_uz} tasdiqlaydi. Modelning aniq nomini yozing, men joriy bazadagi narxlarni ko‘rsataman.",
    },
    "request_choose_attribute": {
        "ru": "Вы выбрали {model}.\n\nВыберите {attribute_label}:\n\n{variants}\n\nУказанные суммы — цены из текущей базы. Точную цену и наличие подтвердит менеджер.",
        "uz": "Siz {model} modelini tanladingiz.\n\n{attribute_label}ni tanlang:\n\n{variants}\n\nKo‘rsatilgan summalar — joriy bazadagi narxlar. Aniq narx va mavjudlikni menejer tasdiqlaydi.",
    },
    "request_choose_color": {
        "ru": "Вы выбрали {model}.\n\nВыберите цвет или нажмите «Цвет не важен»:\n\n{variants}\n\nВыбор не подтверждает наличие товара.",
        "uz": "Siz {model} modelini tanladingiz.\n\nRangni tanlang yoki «Rang muhim emas» tugmasini bosing:\n\n{variants}\n\nTanlov mahsulot mavjudligini tasdiqlamaydi.",
    },
    "request_choose_fulfillment": {
        "ru": "Как вы хотите получить товар?\n\nДоставка по Ташкенту бесплатная и обычно занимает 2–3 часа после подтверждения заказа, цены и наличия менеджером.\n\nСамовывоз возможен только после подтверждения цены и наличия. Пожалуйста, не приезжайте без подтверждения.",
        "uz": "Mahsulotni qanday olishni xohlaysiz?\n\nToshkent bo‘ylab yetkazib berish bepul va buyurtma, narx hamda mavjudlik menejer tomonidan tasdiqlangandan keyin odatda 2–3 soat davom etadi.\n\nDo‘kondan olib ketish faqat narx va mavjudlik tasdiqlanganidan keyin mumkin. Tasdiqsiz kelmang.",
    },
    "request_delivery_phone": {
        "ru": "Для доставки нужен номер телефона. Напишите его текстом, например: +998 90 123 45 67, или вручную отправьте контакт через Telegram.\n\nНомер будет использован только для связи по этой заявке.",
        "uz": "Yetkazib berish uchun telefon raqami kerak. Uni matn ko‘rinishida yozing, masalan: +998 90 123 45 67, yoki Telegram orqali kontaktni qo‘lda yuboring.\n\nRaqam faqat shu murojaat bo‘yicha bog‘lanish uchun ishlatiladi.",
    },
    "request_pickup_contact": {
        "ru": "Для самовывоза телефон не обязателен: менеджер может ответить вам в Telegram. При желании можно добавить номер телефона.",
        "uz": "Do‘kondan olib ketish uchun telefon raqami majburiy emas: menejer sizga Telegram orqali javob berishi mumkin. Xohlasangiz, telefon raqamini qo‘shishingiz mumkin.",
    },
    "request_delivery_location": {
        "ru": "Отправьте адрес доставки одним из способов:\n• геолокацию через скрепку Telegram;\n• ссылку Google или Yandex Maps;\n• адрес текстом.\n\nНе отправляйте код домофона или другую секретную информацию.",
        "uz": "Yetkazib berish manzilini quyidagi usullardan biri orqali yuboring:\n• Telegramdagi skrepka orqali geolokatsiya;\n• Google yoki Yandex Maps havolasi;\n• manzilni matn ko‘rinishida.\n\nDomofon kodi yoki boshqa maxfiy ma’lumotlarni yubormang.",
    },
    "request_review": {
        "ru": "Проверьте заявку для менеджера:\n\n{details}\n\nЦена и наличие ещё не подтверждены. Эта заявка не оформляет заказ и не резервирует товар.",
        "uz": "Menejer uchun murojaat ma’lumotlarini tekshiring:\n\n{details}\n\nNarx va mavjudlik hali tasdiqlanmagan. Bu murojaat buyurtmani rasmiylashtirmaydi va mahsulotni band qilmaydi.",
    },
    "request_saved_delivery": {
        "ru": "Данные для доставки сохранены и переданы менеджеру. Он ответит вам {manager_time_phrase_ru}.\n\nТочную цену, наличие и возможность доставки подтвердит менеджер. Заказ пока не оформлен, товар не зарезервирован.",
        "uz": "Yetkazib berish ma’lumotlari saqlandi va menejerga yuborildi. Menejer sizga {manager_time_phrase_uz} javob beradi.\n\nAniq narx, mavjudlik va yetkazib berish imkoniyatini menejer tasdiqlaydi. Buyurtma hali rasmiylashtirilmagan va mahsulot band qilinmagan.",
    },
    "request_saved_pickup": {
        "ru": "Данные для самовывоза сохранены и переданы менеджеру. Он ответит вам {manager_time_phrase_ru}.\n\nСамовывоз возможен только после подтверждения цены и наличия менеджером. Пожалуйста, не приезжайте без подтверждения. Заказ пока не оформлен, товар не зарезервирован.",
        "uz": "Do‘kondan olib ketish ma’lumotlari saqlandi va menejerga yuborildi. Menejer sizga {manager_time_phrase_uz} javob beradi.\n\nDo‘kondan olib ketish faqat menejer narx va mavjudlikni tasdiqlaganidan keyin mumkin. Tasdiqsiz kelmang. Buyurtma hali rasmiylashtirilmagan va mahsulot band qilinmagan.",
    },
    "request_partial_saved": {
        "ru": "Имеющиеся данные сохранены для менеджера. Не указано: {missing_fields}. Менеджер уточнит детали {manager_time_phrase_ru}.\n\nЭто не подтверждение заказа и не резерв товара.",
        "uz": "Mavjud ma’lumotlar menejer uchun saqlandi. Ko‘rsatilmagan: {missing_fields}. Menejer tafsilotlarni {manager_time_phrase_uz} aniqlashtiradi.\n\nBu buyurtma tasdiqlanganini yoki mahsulot band qilinganini anglatmaydi.",
    },
    "request_cancelled": {
        "ru": "Заявка отменена. Телефон и точная локация из черновика удалены. Если захотите начать заново, напишите название модели.",
        "uz": "Murojaat bekor qilindi. Qoralamadagi telefon va aniq lokatsiya o‘chirildi. Qaytadan boshlash uchun model nomini yozing.",
    },
    "request_stale_button": {
        "ru": "Этот экран уже устарел. Используйте последние кнопки в чате.",
        "uz": "Bu ekran eskirgan. Chatdagi eng so‘nggi tugmalardan foydalaning.",
    },
    "request_invalid_phone": {
        "ru": "Не удалось распознать номер. Напишите его, например: +998 90 123 45 67, или вручную отправьте контакт через Telegram.",
        "uz": "Telefon raqamini aniqlay olmadim. Uni masalan, +998 90 123 45 67 ko‘rinishida yozing yoki Telegram orqali kontaktni qo‘lda yuboring.",
    },
    "request_invalid_location": {
        "ru": "Не удалось распознать адрес. Отправьте геолокацию, безопасную ссылку Google/Yandex Maps или напишите адрес текстом.",
        "uz": "Manzilni aniqlay olmadim. Geolokatsiya, xavfsiz Google/Yandex Maps havolasi yoki manzilni matn ko‘rinishida yuboring.",
    },
}

for _alias, _canonical in TEMPLATE_ALIASES.items():
    TEMPLATES[_alias] = TEMPLATES[_canonical]


@dataclass(frozen=True)
class TemplateOverride:
    code: str
    enabled: bool
    scope: str
    priority: int
    text_ru: str
    text_uz: str
    cooldown_minutes: int = 0
    notes: str = ""

    def text(self, language: str) -> str:
        return self.text_uz if language == "uz" else self.text_ru


class _SafeValues(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


def normalize_template_code(code: str) -> str:
    clean = str(code or "").strip()
    return TEMPLATE_ALIASES.get(clean, clean)


def validate_template_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("template text is empty")
    try:
        fields = [field for _, field, _, _ in Formatter().parse(text) if field]
    except ValueError as exc:
        raise ValueError("template has malformed placeholders") from exc
    for field in fields:
        if field not in ALLOWED_PLACEHOLDERS:
            raise ValueError(f"unsupported template placeholder: {field}")


def builtin_templates() -> Mapping[str, Mapping[str, str]]:
    canonical = {
        code: MappingProxyType(dict(texts))
        for code, texts in TEMPLATES.items()
        if code not in TEMPLATE_ALIASES
    }
    return MappingProxyType(canonical)


def render_template(
    code: str,
    language: str,
    *,
    overrides: Mapping[str, TemplateOverride] | None = None,
    settings: Mapping[str, Any] | None = None,
    values: Mapping[str, Any] | None = None,
) -> str | None:
    canonical = normalize_template_code(code)
    language = language if language in {"ru", "uz"} else "ru"
    override = (overrides or {}).get(canonical)
    if override is not None:
        if not override.enabled:
            return None
        text = override.text(language)
    else:
        pair = TEMPLATES.get(canonical)
        if pair is None:
            raise KeyError(canonical)
        text = pair[language]
    validate_template_text(text)
    catalog_url = str((settings or {}).get("catalog_url") or CATALOG_URL)
    substitutions = _SafeValues(catalog_url=catalog_url)
    substitutions.update(values or {})
    return text.format_map(substitutions)


def render(code: str, language: str, **values: Any) -> str:
    """Render a built-in fallback without a network dependency."""
    result = render_template(code, language, values=values)
    if result is None:
        raise RuntimeError("built-in template is disabled")
    return result
