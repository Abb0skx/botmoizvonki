# Telegram Business bot

This feature is isolated in `telegram_business/` and mounted at `POST /webhooks/telegram-business`.
It uses a separate bot token and a separate SQLite database. It does not call
`readBusinessMessage`, so receiving and saving a webhook does not mark the chat read.

## Coolify

Copy the `TELEGRAM_BUSINESS_*`, `BUSINESS_*`, `GOOGLE_BUSINESS_*`,
`GOOGLE_SERVICE_ACCOUNT_*`, and `PRODUCT_*` variables from `.env.example` into
Coolify. Keep `TELEGRAM_BUSINESS_ENABLED=false` until the connection and webhook
are ready. The existing Google credential mechanism remains supported; never put
the service-account JSON in Git.

`TELEGRAM_BUSINESS_BOT_ID` may be left empty when the token has the normal
`<numeric bot id>:<secret>` form; otherwise set it explicitly. Configure
`BUSINESS_WORKDAYS` with ISO weekday numbers (`1` is Monday, `7` is Sunday).
Startup validates time ranges, timezone, limits, webhook-secret characters, and
the numeric bot ID derived from the token before enabling the integration. During
manual setup, call Bot API `getMe` (or use the provided `get_me()` adapter) once
and verify that its `id` equals `TELEGRAM_BUSINESS_BOT_ID`.

The approved product adapter reads the project's existing Google `bot_prices` and
`bot_settings` sheets (configured by the existing `INSTAGRAM_PRODUCTS_*`
variables). `PRODUCT_SOURCE=existing_google_bot_prices` documents this choice.
If that source is unavailable, empty, or stale, the bot sends no price and leaves
the conversation for a manager.

`PRODUCT_URLS_PATH` points to the existing `Bot_URLS.xlsx`. Its `product_id` values
are mapped to the first trusted `https://t.me/...` post, matching `Seller_Bot.py`.
For an exact match the model name opens that post; for several matches each model
with an approved mapping has its own highlighted link/button. A catalog model with
no trusted row in `Bot_URLS.xlsx` remains safe plain text instead of receiving an
invented URL, so that file must be completed if every catalog item must be
clickable. Link previews and photos are intentionally disabled, so the reply
remains compact. There is no approved standalone `photo_url` or bot-specific
`file_id` in the current project, and the bot does not invent or scrape image URLs.
If `/app/data` is mounted as a persistent volume, make sure `Bot_URLS.xlsx` is
present inside that mounted directory; an empty mount hides the copy packaged in
the image.

## BotFather and Telegram Business

1. Create a new bot in BotFather; do not reuse the calls or delivery bot.
2. Enable **Secretary Mode** (called Business Mode in older BotFather/docs) for
   this bot. In the TEXNIKACH account's Business chatbot settings, connect this
   bot only to the intended private-chat recipients and grant only `can_reply`.
   Leave `can_read_messages`, deletion, profile, stories, gifts, and Stars rights
   disabled.
3. Set the webhook to
   `https://bot.texnikach.uz/webhooks/telegram-business`, include a strong
   `secret_token`, and subscribe to `business_connection`, `business_message`,
   `edited_business_message`, `deleted_business_messages`, and `callback_query`.
   In the `setWebhook` payload use exactly those five values in
   `allowed_updates`; do not set `drop_pending_updates=true` during rollout.
4. Put the resulting connection ID and secret in Coolify, then enable the feature.
5. Disable Telegram's built-in Greeting/Away messages to avoid duplicate replies.
6. Run `getMe` with the new bot token and verify the returned numeric ID against
   `TELEGRAM_BUSINESS_BOT_ID` before enabling the webhook.

Before production, test with two Telegram accounts that the incoming message
remains unread (one check), a bot answer carries `sender_business_bot`, a manual
profile answer activates manager lock, and the bot can reply only inside Telegram's
24-hour Business reply window.

The Telegram adapter exposes safe `getMe` and `getBusinessConnection` checks.
Its exceptions carry `status`, `retryable`, and `retry_after` metadata for
the durable scheduler, but never include the bot-token URL. No code path calls
`readBusinessMessage`.

Night-wizard buttons use only `InlineKeyboardMarkup`. Their `callback_data` is
an opaque `nr1:<token>` value of at most 64 bytes; it never contains a chat id,
model, phone, address, or other client data. Reply keyboards, `request_contact`,
and `request_location` are deliberately unsupported.

## Ночной сценарий заявки

В интервале `20:00–09:30` найденный товар переводится в пошаговый черновик:

1. при неоднозначном поиске клиент выбирает точную модель; названия ведут на
   доверенные Telegram-посты, отдельные фотографии бот не отправляет;
   технические версии одной семьи (например, SIM/eSIM или Lightning/USB-C)
   также выводятся отдельными кнопками и не объединяются молча;
2. если в утверждённом каталоге есть варианты `GB/TB`, бот предлагает память;
3. если колонка памяти содержит значения `mm`, шаг называется «Размер»;
4. при отсутствии памяти/размера этот шаг автоматически пропускается;
5. реальные цвета выводятся кнопками вместе с «Цвет не важен»; при отсутствии
   цветов шаг также пропускается;
6. клиент выбирает доставку или самовывоз;
7. для доставки обязательны телефон и локация/адрес, для самовывоза телефон
   необязателен и связь может остаться в текущем Telegram-чате;
8. клиент проверяет сводку и передаёт её менеджеру.

Черновик не является оформленным заказом, не резервирует товар и не подтверждает
цену, наличие, доставку или самовывоз. Телефон можно написать текстом или вручную
отправить как Contact. Локацию можно прислать через скрепку Telegram, безопасной
ссылкой Google/Yandex Maps, координатами или текстовым адресом. Business-сообщения
не поддерживают кнопки `request_contact`/`request_location`, поэтому бот их не
имитирует.

Состояние каждого шага, версия экрана и одноразовые callback-токены хранятся в
SQLite. Старые и чужие кнопки отклоняются, ответ менеджера закрывает черновик, а
в 09:30 незавершённый черновик помечается как переданный менеджеру с уже
полученными данными. Отмена удаляет телефон и точную локацию из черновика.

## Persistence

The database creates: `business_connections`, `business_updates`,
`business_clients`, `business_sessions`, `business_messages`, `response_cycles`,
`scheduled_actions`, `sheets_outbox`, `business_errors`, and
`business_model_choices`, plus the lightweight `business_manager_fences` used to
stop an already queued automatic action as soon as a manual webhook is persisted,
and `business_outbound_deliveries` for stable automatic-reply delivery fencing.
The night request flow additionally uses `business_requests`,
`business_request_events`, `business_callback_tokens`, and
`business_callback_receipts` for durable drafts and versioned inline buttons.
`business_runtime_leases` serializes Google read/modify/write work across app
processes. Migrations are additive/idempotent and never remove existing data. Webhook updates, debounce,
final, delayed credit, and Google sync live in SQLite. Workers claim rows with
expiring leases, generation fencing, and retry backoff, so a restart cannot strand
a timer or acknowledge an update only in memory.

Telegram `sendMessage` has no caller-provided idempotency key. For a fenced
automatic reply, a network interruption, malformed success, HTTP 408, or HTTP 5xx
has an unknown transport outcome and is therefore treated as delivered rather
than sent again. This prevents duplicate customer messages at the safer cost of
possibly omitting that one automatic reply. The conversation remains assigned to
a manager either way. A definite Telegram 429 remains safely retryable with its
`Retry-After` delay.

Messages that clearly refer to an already placed order or an active delivery are
handed to a manager and set the client's permanent `bot_paused` flag. The same
safe hook, `BusinessRepository.set_bot_paused(chat_id, True, now, reason)`, is
available for staff exclusions and a future delivery integration. The current
delivery database has no reliable Telegram Business `chat_id`/user mapping, so
the project deliberately does not guess that relationship. Until such a mapping
exists, staff can pause an already confirmed order through that hook or an
operator tool built on top of it.

## Google workbook

The target workbook is `13ZFPrYqtV9TQxzNEWsIgw3mX90eZny6sXGvDfpnTLeE` and the
used tabs are `Автоответы`, `Интенты`, `Настройки`, `Диалоги`, `Сообщения`,
`Заявки`, `Статистика`, and `Ошибки`. The workbook must remain private to its owner and staff.
Share it directly with the Google service-account email as **Editor**; never enable
public or “anyone with the link” access.
Workbook initialization/synchronization is not run merely by importing the app:
with the feature enabled and valid credentials, the durable worker initializes it
on its first sync cycle.

Initialization is idempotent: it adds missing sheets/headers and seeds approved
default rows by their stable code/key only when they are missing. Existing response
text and operator edits are never overwritten. The runtime reads `Автоответы`, `Интенты`, and
`Настройки` through a five-minute cache. A malformed row keeps its previous valid
value; a Google outage keeps the last successful snapshot; built-in approved
templates are the final fallback. Outbox upserts support `Сообщения`, `Диалоги`,
`Заявки`, `Статистика`, and `Ошибки` with stable keys and exponential retry
managed by SQLite. `Заявки` contains the selected model/option/color,
fulfillment method, safe location, status and source-price metadata. The phone
is masked in Sheets; its full value remains only in protected SQLite and the
private Telegram chat.

Every web worker must use the same `BUSINESS_DB_PATH` on the same persistent
volume. The SQLite sync lease coordinates processes sharing that file; it cannot
coordinate Coolify replicas with separate local volumes. Use one application
replica unless all workers truly share the configured database file.

All client-authored text and captions are sanitized before SQLite/outbox storage:
long payment/account numbers, IBAN, expiry dates, and CVV values are redacted.
Structured Telegram IDs are preserved for idempotency. Tokens and service-account
credentials are never included in stored errors or logs.
