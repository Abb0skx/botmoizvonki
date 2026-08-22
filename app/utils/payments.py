COLLECT_ON_DELIVERY = "collect_on_delivery"
PAID_AT_ASSEMBLY = "paid_at_assembly"

PAYMENT_LABELS = {
    COLLECT_ON_DELIVERY: "💵 Получить при доставке",
    PAID_AT_ASSEMBLY: "✅ Оплачено при сборе товара",
}


def normalize_payment(value: str) -> str:
    normalized = value.strip().casefold()
    for payment_status, label in PAYMENT_LABELS.items():
        if label.casefold() == normalized:
            return payment_status
    raise ValueError("Выберите вариант оплаты кнопкой")


def payment_label(payment_status: str | None) -> str:
    return PAYMENT_LABELS.get(payment_status or COLLECT_ON_DELIVERY, PAYMENT_LABELS[COLLECT_ON_DELIVERY])
