"""Стабильные коды и переводы полей формы отзывов."""

CATEGORIES = {
    "manager": {
        "ru": "Как вы оцениваете работу менеджера?",
        "uz": "Menejer ishini qanday baholaysiz?",
    },
    "price": {
        "ru": "Как вы оцениваете наши цены?",
        "uz": "Narxlarimizni qanday baholaysiz?",
    },
    "availability": {
        "ru": "Как вы оцениваете наличие и скорость поиска товара?",
        "uz": "Mahsulot mavjudligi va uni topish tezligini qanday baholaysiz?",
    },
    "delivery": {
        "ru": "Как вы оцениваете доставку?",
        "uz": "Yetkazib berishni qanday baholaysiz?",
    },
    "courier": {
        "ru": "Как вы оцениваете работу курьера?",
        "uz": "Kuryer ishini qanday baholaysiz?",
    },
    "product": {
        "ru": "Как вы оцениваете полученный товар?",
        "uz": "Qabul qilgan mahsulotingizni qanday baholaysiz?",
    },
    "overall": {
        "ru": "Как вы в целом оцениваете Texnikach?",
        "uz": "Texnikach xizmatini umumiy qanday baholaysiz?",
    },
}

REASONS = {
    "manager": {
        "slow_response": ("Долго отвечал", "Javob berishi uzoq davom etdi"),
        "no_answer": ("Не смог ответить на вопрос", "Savolga javob bera olmadi"),
        "wrong_information": ("Дал неверную информацию", "Noto‘g‘ri ma’lumot berdi"),
        "no_help_choice": ("Не помог с выбором", "Tanlashda yordam bermadi"),
        "wrong_price": ("Цена оказалась другой", "Narx boshqacha bo‘lib chiqdi"),
        "no_stock": ("Не было нужного товара", "Kerakli mahsulot yo‘q edi"),
        "rude": ("Невежливое общение", "Muomala qo‘pol bo‘ldi"),
        "slow_order": ("Долго оформляли заказ", "Buyurtma uzoq rasmiylashtirildi"),
        "other": ("Другое", "Boshqa"),
    },
    "price": {
        "higher_than_expected": ("Цена выше, чем ожидал", "Narx kutganimdan yuqori"),
        "competitor_cheaper": ("У конкурентов дешевле", "Raqobatchilarda arzonroq"),
        "price_changed": ("Цена изменилась", "Narx o‘zgardi"),
        "different_from_listed": ("Цена отличалась от указанной", "Narx ko‘rsatilganidan farq qildi"),
        "no_discount": ("Не было скидки", "Chegirma bo‘lmadi"),
        "unclear_price": ("Цена была непонятна", "Narx tushunarsiz edi"),
        "other": ("Другое", "Boshqa"),
    },
    "availability": {
        "out_of_stock": ("Товара не оказалось", "Mahsulot mavjud emas edi"),
        "slow_search": ("Долго искали товар", "Mahsulot uzoq izlandi"),
        "wrong_color": ("Не было нужного цвета", "Kerakli rang yo‘q edi"),
        "wrong_variant": ("Не было нужной памяти / версии", "Kerakli xotira yoki versiya yo‘q edi"),
        "alternative_only": ("Предложили только другой вариант", "Faqat boshqa variant taklif qilindi"),
        "slow_confirmation": ("Долго подтверждали наличие", "Mavjudlik uzoq tasdiqlandi"),
        "other": ("Другое", "Boshqa"),
    },
    "delivery": {
        "too_slow": ("Доставка заняла слишком долго", "Yetkazib berish juda uzoq davom etdi"),
        "courier_late": ("Курьер опоздал", "Kuryer kechikdi"),
        "no_delay_warning": ("Не предупредили о задержке", "Kechikish haqida ogohlantirishmadi"),
        "courier_unreachable": ("Было сложно связаться с курьером", "Kuryer bilan bog‘lanish qiyin bo‘ldi"),
        "address_problem": ("Возникла проблема с адресом", "Manzil bilan muammo yuz berdi"),
        "inconvenient_time": ("Неудобное время доставки", "Yetkazib berish vaqti noqulay edi"),
        "other": ("Другое", "Boshqa"),
    },
    "courier": {
        "rude": ("Невежливое общение", "Muomala qo‘pol bo‘ldi"),
        "rushed_inspection": ("Курьер торопил при проверке", "Kuryer tekshirishda shoshiltirdi"),
        "careless_handling": ("Неаккуратно обращался с товаром", "Mahsulotga ehtiyotsiz munosabatda bo‘ldi"),
        "payment_problem": ("Возникли сложности с оплатой", "To‘lovda qiyinchilik bo‘ldi"),
        "no_receipt_help": ("Не помог при получении товара", "Mahsulotni qabul qilishda yordam bermadi"),
        "unreachable": ("Было сложно связаться", "Bog‘lanish qiyin bo‘ldi"),
        "other": ("Другое", "Boshqa"),
    },
    "product": {
        "damaged_package": ("Повреждена упаковка", "Qadoq shikastlangan"),
        "external_defect": ("Есть внешние дефекты", "Tashqi nuqsonlar bor"),
        "wrong_color": ("Не тот цвет", "Rangi boshqa"),
        "wrong_variant": ("Не та версия / память", "Versiya yoki xotira boshqa"),
        "package_contents": ("Проблема с комплектацией", "Komplektatsiya bilan muammo"),
        "device_problem": ("Проблема с устройством", "Qurilma bilan muammo"),
        "below_expectations": ("Товар не соответствовал ожиданиям", "Mahsulot kutilganidek emas"),
        "other": ("Другое", "Boshqa"),
    },
    "overall": {
        "manager": ("Менеджер", "Menejer"),
        "price": ("Цена", "Narx"),
        "availability": ("Наличие товара", "Mahsulot mavjudligi"),
        "service_speed": ("Скорость обслуживания", "Xizmat ko‘rsatish tezligi"),
        "delivery": ("Доставка", "Yetkazib berish"),
        "courier": ("Курьер", "Kuryer"),
        "product": ("Товар", "Mahsulot"),
        "information": ("Информация на сайте / Telegram", "Sayt / Telegram’dagi ma’lumot"),
        "other": ("Другое", "Boshqa"),
    },
}


def reason_exists(category: str, code: str) -> bool:
    return code in REASONS.get(category, {})
