# Customer reviews

Изолированное пространство системы оценки качества Texnikach.

- Основной FastAPI по-прежнему находится в `botmoizvonki.py`.
- Модуль использует отдельную SQLite-базу `reviews.db`.
- База создаётся только явным вызовом `init_reviews_db()`; импорт пакета не меняет файловую систему.
- Стабильные коды категорий и причин находятся в `catalog.py`, отображаемые тексты — на RU и UZ.
- Менеджеры хранятся в таблице `managers`; начальные записи: `Olmas`, `Otabek`, `MuhammadAli` и `Abbos`.

## Переменные окружения

- `REVIEWS_DB_PATH` (production default: `/app/data/reviews.db`)
- `REVIEWS_COMPLAINT_PHONE`
- `REVIEWS_COMPLAINT_TELEGRAM`
- `REVIEWS_TELEGRAM_BOT_TOKEN`
- `REVIEWS_TELEGRAM_CHAT_ID`
- `REVIEWS_SITE_URL`
- `REVIEWS_CRITICAL_RATING` (default: `2`)
- `REVIEWS_RATE_LIMIT` (default: `5`)
- `REVIEWS_RATE_WINDOW_SECONDS` (default: `900`)

## Реализовано

- `GET /rating` и совместимый адрес `GET /review`;
- `POST /api/reviews` с серверной валидацией;
- mobile-first форма RU/UZ;
- оценки, динамические причины, несколько менеджеров и пропуск доставки;
- нормализация телефона, honeypot, CSRF и мягкий IP rate limit;
- структурированное сохранение и флаг `needs_attention`;
- фоновое Telegram-уведомление для критических оценок;
- минимальное подключение `reviews_router` к основному FastAPI.

Перед production deployment нужно задать ENV в Coolify и убедиться, что
`/app/data` подключён как persistent storage.
Готовый список переменных без секретных значений находится в `.env.example`.
