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
   bot only to the intended private-chat recipients and grant `can_reply` and
   `can_delete_sent_messages`. The deletion right is used only for the bot's own
   previous delivery-status messages. Leave `can_delete_all_messages`, profile,
   `can_read_messages`, stories, gifts, and Stars rights disabled.
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

Явный русский или узбекский язык сохраняется из каждого входящего сообщения
клиента даже при активном manager lock: блокировка запрещает автоответ, но не
обновление контекста. Нейтральные сообщения — модель, номер, Contact или
локация — сохранённый язык не меняют. Поэтому служебный статус доставки
использует последний язык, на котором клиент действительно писал.

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
7. для доставки обязательна локация/адрес; телефон необязателен, и связь может
   остаться в текущем Telegram-чате; для самовывоза телефон также необязателен;
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

## Уведомления о состоянии доставки

Интеграция выключена по умолчанию. После проверки защищённого внутреннего API
доставки настройте:

```dotenv
BUSINESS_DELIVERY_NOTIFICATIONS_ENABLED=true
BUSINESS_DELIVERY_NOTIFICATIONS_URL=http://texnikach-delivery-stats:8080
BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN=тот-же-секрет-что-MONITORING_DELIVERY_SERVICE_TOKEN
BUSINESS_DELIVERY_NOTIFICATIONS_POLL_SECONDS=30
BUSINESS_DELIVERY_NOTIFICATIONS_MAX_EVENT_AGE_HOURS=24
```

Пустые `BUSINESS_DELIVERY_NOTIFICATIONS_URL` и
`BUSINESS_DELIVERY_NOTIFICATIONS_TOKEN` используют соответственно
`MONITORING_DELIVERY_BASE_URL` и `MONITORING_DELIVERY_SERVICE_TOKEN`. URL должен
указывать на внутренний HTTP(S)-адрес без логина, пароля, query-параметров или
fragment. Секрет передаётся только как Bearer token и не записывается в логи или
SQLite. Проще и безопаснее оставить обе специальные переменные пустыми и
использовать fallback. Если токен задан явно, он обязан точно совпадать с
`MONITORING_DELIVERY_SERVICE_TOKEN` контейнера `delivery-stats`: endpoint не
принимает отдельный второй секрет. Оба контейнера должны находиться в одной
Docker-сети, где имя `texnikach-delivery-stats` разрешается во внутренний адрес.
Открытый публичный HTTP запрещён; для внешнего адреса используйте HTTPS.

Business-бот связывает доставку с чатом только по номеру, который ранее прислал
сам клиент во входящем Business-сообщении. Один чат может содержать несколько
номеров. Если один номер найден в нескольких чатах либо два номера одного заказа
ведут в разные чаты, совпадение считается неоднозначным и автоматического
сообщения не будет. Удалённые и заменённые при редактировании сообщения номера,
платёжные данные, исходящие сообщения менеджера/бота и чужой Telegram Contact не
используются для сопоставления.

Написанный текстом номер считается заявленным клиентом, но Telegram не умеет
доказать, кому он принадлежит. Поэтому совпадение используется только при одном
единственном подходящем чате, активном разрешении `can_reply` и наличии в этом
чате хотя бы одного предыдущего реального исходящего сообщения менеджера или
Business-бота. Полный номер хранится только в защищённой SQLite/Telegram;
свободный текст листа `Сообщения` получает маскированный номер.
После первой возможной отправки заказ навсегда привязывается к выбранному чату:
последующие изменения телефонных совпадений не могут перенести его уведомления
другому Telegram-пользователю.

Telegram разрешает Business-боту ответить только в течение 24 часов после
последнего входящего сообщения клиента. Поэтому наличие номера и старой
переписки само по себе не даёт права на отправку. Перед каждой отправкой также
проверяются активное Business-подключение, `can_reply` и постоянный
`bot_paused`. Временный manager lock останавливает разговорные автоответы, но
не задерживает служебные статусы доставки. Пауза с причиной `active_order`
блокирует обычного ночного помощника, но не служебное уведомление о самой
доставке. Номер служит только для поиска сохранённых `chat_id` и
`business_connection_id`; Telegram не позволяет найти или открыть чат по одному
номеру телефона.

Клиенту отправляются только короткие статусы без внутреннего номера заказа:

- `pending` — ожидаем курьера;
- `picked_up` — назначенный этому заказу курьер забрал товар; клиент видит его имя;
- `on_way` — назначенный этому заказу курьер выехал; клиенту предлагается быть
  по указанному адресу и подготовиться к получению, также отправляются имя и
  номер именно этого курьера;
- `completed` — товар доставлен; в том же коротком сообщении клиенту предлагается
  оценить работу по ссылке `https://bot.texnikach.uz/review`;
- `cancelled` — активная доставка подтверждённо отменена.

Контакт выбирается только по Telegram ID курьера из доверенного справочника
доставки: Muzrob Oka — `+998948765070`, Olmas — `+998900979898`, Abbos —
`+998901333999`. Имя из заказа не используется для сопоставления. Если ID
отсутствует, неизвестен или противоречит назначению, контакт другого курьера не
подставляется и уведомление с ошибочным номером не отправляется.

Сначала бот успешно отправляет новый статус, фиксирует его `message_id` в SQLite
и только затем ставит предыдущие статусы этой же доставки в устойчивую очередь
удаления. Удаляются исключительно сообщения, которые этот Business-бот сам
отправил с шаблоном `delivery_status_*`; сообщения клиента и ручные ответы
менеджера никогда не выбираются. Очередь переживает перезапуск и повторяет
временные ошибки с backoff. Поэтому в обычной цепочке «ожидаем курьера → курьер
забрал товар → курьер выехал → товар доставлен» после завершения остаётся только
сообщение о доставке и ссылка на отзыв.

Для удаления Telegram требует `can_delete_sent_messages`; `can_reply` одного
недостаточно. Bot API обычно не удаляет сообщения старше 48 часов. Если право
отозвано или доставка между статусами длится дольше этого срока, новый статус всё
равно отправляется, но Telegram может не позволить убрать старый. Даже если у
подключения уже есть более широкое право `can_delete_all_messages`, код никогда не
выбирает произвольные сообщения и не вызывает `readBusinessMessage`.

Служебные `draft`, `awaiting_photo`, `awaiting_amount`, переназначение курьера,
изменение полей без смены статуса и обратные/восстанавливающие переходы клиенту
не показываются. Один и тот же публичный статус одного заказа отправляется не
более одного раза. После временной недоступности источника устаревшие
промежуточные статусы не рассылаются подряд: остаётся только последнее актуальное
состояние заказа.

Первый успешный опрос только сохраняет текущую границу событий и не рассылает
исторические доставки. Последующие события и состояние отправок фиксируются в
основной Business SQLite, поэтому перезапуск не создаёт дубликаты. Автоматический
backfill старых событий по умолчанию отсутствует. Уведомление старше
`BUSINESS_DELIVERY_NOTIFICATIONS_MAX_EVENT_AGE_HOURS` не отправляется. До четырёх
сообщений бота всего (обычных ответов и служебных статусов вместе) одному чату
могут быть отправлены за десять минут; остальные служебные статусы остаются в
устойчивой очереди. Telegram `Retry-After` останавливает всю очередь отправки до
указанного времени.

## Persistence

The database creates: `business_connections`, `business_updates`,
`business_clients`, `business_sessions`, `business_messages`,
`business_chat_phones`, `response_cycles`, `scheduled_actions`, `sheets_outbox`,
`business_errors`, `business_integration_state`,
`delivery_status_notifications`, `delivery_status_message_deletions`, and
`business_model_choices`, plus the
lightweight `business_manager_fences` used to
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
safe hook, `BusinessRepository.set_bot_paused(chat_id, True, now, reason)`, remains
available for staff exclusions. Delivery-status matching never guesses a Telegram
identity from a phone number: it uses only durable phone evidence from that
client's existing Business chat and fails closed on ambiguity.

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

## Цветные папки менеджеров (MTProto)

Bot API не умеет добавлять личные чаты в папки. Опциональный модуль
`telegram_folder_manager` подключается как отдельное Telegram-устройство через
MTProto и использует только методы `messages.getDialogFilters`,
`messages.updateDialogFilter` и `messages.toggleDialogFilterTags`. Он не вызывает
`messages.readHistory`, не отправляет сообщения и не сохраняет их текст.

По умолчанию модуль выключен. Добавьте в Coolify:

```dotenv
TELEGRAM_FOLDER_SYNC_ENABLED=false
TELEGRAM_USER_API_ID=
TELEGRAM_USER_API_HASH=
TELEGRAM_USER_SESSION_PATH=/app/data/texnikach-user.session
TELEGRAM_FOLDER_POLL_SECONDS=5
TELEGRAM_FOLDER_RECONCILE_SECONDS=30
TELEGRAM_FOLDER_BACKFILL_EXISTING=false
TELEGRAM_FOLDER_REOPEN_DONE=true
TELEGRAM_FOLDER_NEW=NEW
TELEGRAM_FOLDER_OLMAS=OLMAS
TELEGRAM_FOLDER_OTABEK=OTABEK
TELEGRAM_FOLDER_ALI=ALI
TELEGRAM_FOLDER_ABBOS=ABBOS
TELEGRAM_FOLDER_DONE=DONE
TELEGRAM_SUPPLIER_SYNC_ENABLED=true
TELEGRAM_SUPPLIER_GROUP_IDS=-1002188560435,-1001173906517,-1001463992108,-1002268274885,-1002480123950,-1001607065824,-1002496061682
TELEGRAM_SUPPLIER_SCAN_SECONDS=21600
TELEGRAM_FOLDER_SUPPLIER=Поставщики
TELEGRAM_FOLDER_SUPPLIER2=Поставщики 2
TELEGRAM_FOLDER_SUPPLIER3=Поставщики 3
TELEGRAM_FOLDER_SUPPLIER4=Поставщики 4
```

`api_id` и `api_hash` берутся на `my.telegram.org` и задаются как скрытые runtime
переменные. Они, файл `*.session`, QR-ссылка и пароль 2FA никогда не должны
попадать в Git, логи или сообщения. `/app/data` обязан быть постоянным диском.

Одноразовая авторизация выполняется в Terminal контейнера, пока синхронизация
ещё выключена:

```bash
python -m telegram_folder_manager.auth
```

Откройте Telegram → Настройки → Устройства → Подключить устройство и считайте
QR-код. После строки `AUTHORIZED` включите `TELEGRAM_FOLDER_SYNC_ENABLED=true`
и перезапустите один экземпляр приложения. При необходимости 2FA-пароль
вводится интерактивно и нигде не сохраняется. Скомпрометированный `.session`
даёт полный доступ к аккаунту; такое устройство нужно немедленно завершить в
Telegram → Настройки → Устройства.

При первом безопасном запуске текущая история не размечается. Только новые
входящие Business-чаты получают `NEW`. Если нужен осознанный разовый backfill,
на один запуск задайте `TELEGRAM_FOLDER_BACKFILL_EXISTING=true`, затем верните
`false`. Папки создаются с цветами и включаются как теги. Чтобы взять клиента,
добавьте его в папку менеджера в обычном Telegram; worker увидит изменение,
уберёт `NEW` и оставит один тег. `DONE` означает завершённый диалог; новое
сообщение клиента снова переводит его в `NEW`.

Назначение также можно поставить без текста переписки командой:

```bash
python -m telegram_folder_manager.cli assign CHAT_ID OLMAS
python -m telegram_folder_manager.cli list
```

Состояние и устойчивые retry хранятся в таблицах
`telegram_folder_assignments`, `telegram_folder_jobs` и
`telegram_folder_state`. Файл сессии не входит в резервные копии репозитория.

### Поставщики из общих групп

Подключённый пользовательский MTProto-аккаунт раз в шесть часов получает список
участников семи выбранных групп со снимка: Malika bozor N1, Malika Akses N1,
MALIKA case No1, Malika DASTAVKA, MALIKA AKSESSUAR, ПАКЕТЛАР Б-44 и
BM Electronics Malika. Сопоставление идёт по Telegram user ID, а не по имени
или номеру телефона. В папки попадают участники, с которыми у аккаунта есть
личный диалог, в том числе начатый до подключения Business-бота. Содержимое
групп и личных сообщений для этой проверки не читается. При недоступности
списка участников старые отметки сохраняются, а сканирование повторяется позже.

У Telegram Premium действует предел 200 чатов в одной папке. Один слот занимает
«Избранное» как служебный элемент, поэтому модуль распределяет поставщиков
между «Поставщики» и пронумерованными продолжениями по 199 личных чатов на
папку. Все четыре раздела получают красный тег; клиентские папки очищаются от
этих чатов. Для найденных
поставщиков Business-бот ставится на паузу. Если после полного сканирования
человек больше не состоит ни в одной из семи групп, он возвращается в `NEW`,
а пауза снимается только если её установила именно эта классификация. При
превышении общей вместимости 796 чатов лишние ID остаются в базе и не получают
автоответов, а ошибка фиксируется в логе до расширения числа папок.

Доказательства членства хранятся без имён и текстов в
`telegram_supplier_group_members`. Файл сессии и другие секреты не копируются.

## Отдельная статистика группы поставщиков

Опциональный `telegram_market_stats` использует уже подключённый MTProto-клиент,
но хранит данные отдельно от Business-бота в
`/app/data/telegram_market_stats.db`. Он читает `Malika bozor N1` без отправки
сообщений и без отметки их прочитанными, определяет спрос/предложение, модели,
память и цвет, а также отмечает заданных конкурентов по устойчивому Telegram ID.

```dotenv
TELEGRAM_MARKET_STATS_ENABLED=false
TELEGRAM_MARKET_GROUP_ID=-1002188560435
TELEGRAM_MARKET_GROUP_TITLE=Malika bozor N1
TELEGRAM_MARKET_STATS_DB_PATH=/app/data/telegram_market_stats.db
TELEGRAM_MARKET_CATALOG_PATH=/app/data/Bot_URLS.xlsx
TELEGRAM_MARKET_POLL_SECONDS=30
TELEGRAM_MARKET_BACKFILL_DAYS=30
TELEGRAM_MARKET_BACKFILL_LIMIT=100000
TELEGRAM_MARKET_BATCH_SIZE=1000
TELEGRAM_MARKET_EDIT_RESCAN_MESSAGES=500
TELEGRAM_MARKET_COMPETITORS_JSON={"213962560":"Mobilon","6243942320":"MixMobiles_1","1780333654":"MixMobiles_2"}
```

`TELEGRAM_FOLDER_SYNC_ENABLED=true` остаётся обязательным: оба модуля используют
один Telegram client и один session-файл, чтобы исключить параллельную запись в
Telethon SQLite session. Первичный backfill выполняется небольшими устойчивыми
порциями и ограничен одновременно числом дней и максимальным количеством
сообщений. Повторные циклы идемпотентны; последние
сообщения перечитываются для учёта редактирования.

Таблицы отдельной базы: `market_sources`, `market_competitors`,
`market_messages`, `market_model_mentions`, `market_checkpoints` и
`market_collector_runs`. Текст нерелевантных сообщений не сохраняется; для
релевантных остаётся только защищённый фрагмент до 500 символов. Карточные и
платёжные данные редактируются перед записью.

Проверка агрегатов без доступа к переписке:

```bash
python -m telegram_market_stats.cli models --days 7
python -m telegram_market_stats.cli competitors --days 30
```

Защищённый веб-отчёт доступен по `https://bot.texnikach.uz/finance` после
входа в общую панель мониторинга. Он читает отдельную SQLite-базу в режиме
read-only и показывает периоды «сегодня», «вчера», 7/30 дней и произвольный
диапазон: популярные модели, уникальных участников, предложения, активность
конкурентов, их модели и дневную динамику. Страница не показывает текст
сообщений группы и не влияет на MTProto-сборщик.

All client-authored text and captions are sanitized before SQLite/outbox storage:
long payment/account numbers, IBAN, expiry dates, and CVV values are redacted.
Structured Telegram IDs are preserved for idempotency. Tokens and service-account
credentials are never included in stored errors or logs.
