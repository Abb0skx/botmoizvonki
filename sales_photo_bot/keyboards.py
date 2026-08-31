from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_PREFIX = "sp"
MANAGER_CALLBACK_PREFIX = f"{CALLBACK_PREFIX}:m:"
BACK_CALLBACK = f"{CALLBACK_PREFIX}:b"
SELLERS = ("Olmas", "Otabek", "Ali", "Abbos")
SELLER_BY_KEY = {seller.casefold(): seller for seller in SELLERS}


def _suffix(
    source_message_id: int | None,
    generation: int,
    signature: str | None,
) -> str:
    if source_message_id is None:
        return ""
    safe_generation = int(generation)
    if safe_generation < 0:
        raise ValueError("Поколение callback не может быть отрицательным")
    safe_signature = str(signature or "").casefold()
    if len(safe_signature) != 12 or any(
        char not in "0123456789abcdef" for char in safe_signature
    ):
        raise ValueError("Для callback source требуется подпись")
    return f":{int(source_message_id)}:{safe_generation}:{safe_signature}"


def manager_keyboard(
    source_message_id: int | None = None,
    generation: int = 0,
    signature: str | None = None,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=seller,
            callback_data=(
                f"{MANAGER_CALLBACK_PREFIX}{seller.casefold()}"
                f"{_suffix(source_message_id, generation, signature)}"
            ),
        )
        for seller in SELLERS
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


def back_keyboard(
    source_message_id: int | None = None,
    generation: int = 0,
    signature: str | None = None,
    manager: str | None = None,
) -> InlineKeyboardMarkup:
    label = "↩️ Назад"
    if manager:
        selected = SELLER_BY_KEY.get(str(manager).casefold())
        if selected:
            label = f"👤 {selected} · {label}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=(
                        f"{BACK_CALLBACK}{_suffix(source_message_id, generation, signature)}"
                    ),
                )
            ]
        ]
    )
