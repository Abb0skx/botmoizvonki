# TEXNIKACH Instagram Inbox

Instagram Direct → постоянная тема Telegram → черновик → подтверждение менеджера → Instagram.
Ни AI, ни обычный текст в группе не отправляют сообщения клиентам. Отправка существует только
как отдельное задание, созданное атомарным подтверждением авторизованного менеджера.

## Архитектура

Python 3.11, FastAPI, aiogram 3, SQLAlchemy 2, Alembic, OpenAI Responses API.
Отдельный сервис и отдельная SQLite-БД, не затрагивающие базы звонков и доставки.
Входящий подписанный webhook синхронно фиксирует сообщение и durable job, затем возвращает 200.
Один worker читает очередь БД, публикует сообщения в Telegram и после debounce=4 секунд
классифицирует пакет. Контекст ограничен AI_CONTEXT_MESSAGES (20 по умолчанию).
Каждое сообщение хранится отдельно, включая медиа, реакции, ответы и исходящие echo-события.

Таблицы: inbox_clients (channel/account/external ID UNIQUE, topic ID UNIQUE, revision),
inbox_messages (external ID UNIQUE, direction, text, attachments, raw, classification),
inbox_drafts (текст, revision, pending/sending/sent/cancelled/failed, подтверждение, результат),
inbox_managers, inbox_jobs (durable очередь с unique key и retry), inbox_telegram_links,
inbox_state (polling offset и heartbeat). Время хранится UTC epoch; менеджеру показывается Ташкент.

Основной ключ клиента — Instagram-scoped ID; username только для отображения.
Одна тема создаётся один раз. Закрытую тему открываем, не создаём другую. Неактивные темы
без ожидающего ответа закрываются через 72 часа (0 отключает). Темы и история автоматически не удаляются.

## Запуск локально

```sh
python3.11 -m venv .venv-inbox
.venv-inbox/bin/pip install -r requirements-instagram-inbox.txt pytest pytest-asyncio
cp instagram_inbox/config.env.example /secure/path/inbox.env
chmod 600 /secure/path/inbox.env
# Экспортируйте значения из env-файла безопасным способом вашего окружения.
export DATABASE_URL=sqlite:////absolute/path/inbox.db
export WORKER_LOCK_PATH=/absolute/path/inbox-worker.lock
.venv-inbox/bin/alembic -c instagram_inbox/alembic.ini upgrade head
.venv-inbox/bin/uvicorn instagram_inbox.main:app --port 8000 --workers 1
.venv-inbox/bin/python -m pytest instagram_inbox/tests -q
```

Для PostgreSQL используйте `postgresql+psycopg://...`, выполните миграции в пустую базу
и перенесите данные отдельно. Слой SQLAlchemy переносим, но эта версия intentionally single-worker:
масштабировать обработчиков до внедрения распределённых leases нельзя. Файл flock блокирует
второй worker на том же томе; Redis/RQ можно внедрить вместо inbox_jobs с сохранением атомарного approve.

## Telegram

Выделенный бот создаётся через BotFather. Группа — приватная supergroup с Forum Topics.
Бот — администратор с can_manage_topics, правами отправки; в закрытых темах бот открывает тему.
Выделенный бот работает через long polling. Не ставьте webhook на него и не запускайте второй poller.
Бот-администратор получает групповые сообщения независимо от privacy mode.
Отвечать могут администраторы группы и Telegram ID из TELEGRAM_MANAGER_IDS (при условии членства).
Основной идентификатор менеджера — числовой Telegram ID, не имя и не username.

- Кнопка ✅ Отправить — единственный способ отправки.
- Reply на входящее сообщение клиента → ручной черновик → отдельное подтверждение.
- Обычный текст, Reply на сообщение коллеги и редактирование уже отправленного Telegram-текста
  не отправляются клиенту. Внутренние заметки не передаются AI.
- 🔄 Другой вариант — новый черновик с теми же правилами и историей; старый отменяется.
- ❌ Не отвечать — отмена с ID менеджера, ничего не отправляется.
- /help, /status, /close.
- /bind CLIENT_ID в существующей теме: привязка после неопределённого результата создания темы.
  Доступно только TELEGRAM_MANAGER_IDS, для клиента без темы со статусом uncertain.

## Meta

Официальный Instagram Messaging API, без пароля и автоматизации Instagram-браузера.
Настройте Meta App, Instagram professional account, нужные messaging permissions/access level,
подписку на messages и связанные события. Доступность профиля, Reels и voice зависит от API/прав.
В URL webhook `https://bot.texnikach.uz/webhooks/instagram` сохранены GET verification и POST.
Проверяем X-Hub-Signature-256 по исходным байтам, account ID и получателя. Секрет обязателен.
Нельзя публиковать сервис с пустым секретом; произвольные POST отклоняются.

Отправка проверяет стандартное 24-часовое окно от последнего сообщения клиента.
HUMAN_AGENT/расширение окна автоматически не используется. Нет API-разрешения или Direct недоступен →
понятная ошибка менеджеру. Частота запросов и отдельные возможности ограничены Meta.
Исходящие ответы сотрудников из приложения Instagram сохраняются по echo; без полученного echo
сервер не может гарантировать знание о внешнем ответе.

Медиа: сохраняем метаданные и доступный URL, публикуем фото/видео/audio, для shared media — ссылку.
Недоступное вложение отмечаем; сохраняется событие. CDN URL могут истекать. Эта версия не архивирует
байты медиа навсегда, не распознаёт голос и не анализирует пиксели: AI видит только тип вложения.
Модель обязана запросить уточнение, если товар из видео неизвестен. Скачивание произвольных URL запрещено.

## AI и факты

OPENAI_API_KEY добавляется только в runtime env. OPENAI_MODEL конфигурируемый.
Два независимых этапа: classifier (structured JSON) → responder (structured proposal).
Очевидные media-only, emoji и благодарности обрабатываются правилами без генерации.
Кредит/рассрочка — всегда вопрос: готовится только отказ, а не коммерческая альтернатива.
Если AI отсутствует/ошибся/не уверен, сообщение остаётся в теме с просьбой ответить вручную.
Приветствие и отказ по кредиту доступны как проверенные шаблоны без OpenAI.

В первой версии KnowledgeProvider содержит подтверждённые правила магазина. Цены, наличие,
варианты, гарантия, адрес и сроки неизвестны, пока не подключён соответствующий provider.
Это намеренное ограничение: старая цена из переписки не является источником свежей цены.
Добавьте реализации KnowledgeProvider.lookup для каталога/цены/наличия; AI не получает функции send.
История отправляется OpenAI без Instagram ID, без CDN подписей, без внутренних заметок; store=false.

## Защита от дублей и восстановление

Approve выполняет условный UPDATE pending→sending, проверяя revision клиента в той же операции.
Только один менеджер получает право отправки. Новый входящий отменяет pending/failed и ещё не начатые
sending. Перед внешним запросом ставится dispatch_started_at. После успеха в одной транзакции
фиксируются sent, ID Meta и outgoing history. Повтор кнопки не создаёт второй send.

Ни Meta Send API, ни Bot API не дают гарантии exactly-once при потере сетевого ответа.
Поэтому после timeout/сбоя процесса результат отмечается неопределённым и повторная отправка
запрещена до проверки. 4xx/429 с явным отказом Meta можно повторить отдельным подтверждением.
Неопределённое создание Telegram-темы не повторяется: /bind связывает уже созданную тему.
Неопределённая публикация Telegram не повторяется автоматически; данные остаются в БД.
Ошибочные задания после пяти попыток отмечаются failed и требуют проверки.

## Production

```sh
docker compose -p texnikach-instagram-inbox -f compose.instagram-inbox.yaml build
docker compose -p texnikach-instagram-inbox -f compose.instagram-inbox.yaml up -d
# После проверки контейнера установите ops/traefik.yaml как
# /data/coolify/proxy/dynamic/instagram-inbox.yaml (на сервере Coolify).
curl -f https://bot.texnikach.uz/instagram/inbox/health
```

Env: `/opt/texnikach-instagram-inbox/.env`, режим 0600. БД: `/opt/texnikach-instagram-inbox/data/inbox.db`.
Контейнер непривилегированный, restart policy, healthcheck, bounded logs, отдельный bind mount.
Постоянный маршрут Traefik file provider направляет только Instagram webhook и health в новый сервис.
Он остаётся активным даже при unhealthy/остановке контейнера: возвращается 502/503 для повтора Meta,
а не старый автоматический Direct-ответ. Docker labels маршрутизации намеренно отключены.
Комментарии через durable очередь пересылаются в `texnikach-calls-service` с новой корректной подписью;
из пересылаемого тела удалены messaging-события, чтобы прежний автоответчик Direct не запустился.
Meta не нужно менять callback URL. До активации проверьте уникальный DNS alias legacy-сервиса.

Скрипты ops/provision_telegram.py и ops/configure_server.py создают настройки без вывода токенов.
Первый запускается в уже авторизованном пользовательском MTProto-окружении, второй — на сервере.
Секрет BotFather сохраняется на защищённом томе; никогда не коммитьте bootstrap JSON/env.
ops/backup.py использует SQLite backup API; ежедневный systemd timer хранит 14 снимков.
Миграции выполняются перед стартом контейнера. Откат кода не удаляет БД; destructive downgrade запрещён.

Удаление файла маршрута вернёт webhook старому приложению и его автоматическим Direct-ответам.
При обслуживании сохраняйте файл маршрута: недоступный backend вернёт 502/503, чтобы Meta повторила событие.
Не удаляйте маршрут, пока прежний Direct-автоответчик не отключён.

## Troubleshooting

- 403 webhook: проверьте META_APP_SECRET/подпись/Meta app, не отключайте проверку.
- Topic failed: проверьте can_manage_topics и supergroup.is_forum; не создавайте тему вручную повторно.
- AI не настроен: добавьте ключ в env, пересоздайте контейнер, проверьте /status.
- Callback без прав: менеджер должен быть участником и администратором либо разрешённым ID.
- Истёк срок ответа: дождитесь нового сообщения клиента; не обходите ограничения Meta.
- `uncertain`: проверьте Instagram/Telegram перед восстановлением; автоматический retry запрещён.
- Queue failed: читайте тип ошибки и ID задания, восстановите внешний сервис; история в БД сохранена.
- Health 503: heartbeat worker/polling устарел, проверьте логи и конфликт второго poller.

API references: https://core.telegram.org/bots/api#createforumtopic,
https://developers.openai.com/api/docs/guides/structured-outputs,
https://www.postman.com/meta/instagram/folder/uxudqu0/send-api.
