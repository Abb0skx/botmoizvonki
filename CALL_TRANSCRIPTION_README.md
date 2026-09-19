# Локальная расшифровка звонков — MVP

## Статус и границы проверки

Реализованы локальный Python API, CLI, два ASR-адаптера, diarization, привязка
слов к говорящим, RU/UZ-классификация реплик, осторожное определение ролей,
JSON/TXT и интеграция с существующей SQLite-очередью/Telegram.

Production-конфигурация 19.09.2026 обрабатывает речевые окна двумя локальными
движками последовательно: GigaAM v3 RNNT для русского и Whisper Medium
Abduqayum/whisper-uzbek-medium-callcenter для узбекской телефонной речи.
Выбор делается по письменности, RU/UZ-лексике, покрытию и confidence; один язык
больше не назначается всему звонку. CJK/арабская письменность и характерные
турецкие/азербайджанские диакритики фильтруются как ложные сегменты. Это не
переводит и не перефразирует смешанную RU/UZ речь.

Unit/integration-тесты используют fakes. Они **не подтверждают качество Whisper
или diarization на настоящих клиентах**. Для приёмки нужен реальный звонок,
скачанные модели и ручная проверка обоих языков, чисел, моделей и ролей.
Нельзя выдавать выдуманный демонстрационный JSON за результат распознавания.

Первоначально обследованный 18.09.2026 production VPS: 2 CPU, 4 GB RAM, около 1 GB available,
swap уже используется. Включать на нём одновременно large-v3/turbo и pyannote
без выделенных ресурсов небезопасно. Код не включает ASR автоматически.

После оптимизации OCR 18.09.2026 доступно около 2 ГБ RAM. Код, CPU-зависимости
и обе модели установлены отдельно в `/opt/texnikach-transcription-20260918`.
Полный тест реального звонка превысил безопасный лимит контейнера 1536 МиБ RAM
+ 256 МиБ swap при загрузке Whisper после diarization. Автообработка и
публикация расшифровок на этом этапе **не включались**. Подробности —
`docs/TRANSCRIPTION_SERVER_TRIAL_20260918.md`.

После перехода на 4 CPU / 8 GB RAM полный offline-тест прошёл: 31.424 сек
аудио обработаны за 262.364 сек, peak RSS 2 251 200 KiB (около 2.15 GiB),
swap не использовался. Получено 19 реплик, роли не определены уверенно.
Это проверка работоспособности, а не подтверждение точности RU/UZ.
CPU-квота теста — 1.5 CPU, int8, diarization batch 1, speech gap 1.5 сек.
Отдельный ограниченный worker и управляемая выкладка web описаны в
`deployment/transcription/README.md`. При большом потоке звонков возможна очередь;
отправка исходных карточек не ждёт распознавания.

## Как встроено в существующую архитектуру

Основной процесс — FastAPI в `botmoizvonki.py`, хранение звонков — SQLite
`calls.db`. В проекте уже были `call_transcriptions`, транзакционная постановка
задания вместе со звонком, lease-token, heartbeat, retry и `TranscriptionWorker`.
Они сохранены. Redis/Celery и отдельная новая очередь не добавлялись.

Изменены:

- `botmoizvonki.py`: локальный адаптер вместо облачного HTTP ASR; аддитивные
  миграции; JSON/TXT; leased Telegram-редактирование; защищённый endpoint.
- `.env.example`: настройки локальных моделей/worker вместо облачных ключей.
- `.gitignore`: исключены локальные веса, отдельное окружение и приватные экспорты.
- `.dockerignore`: сохранены прежние исключения секретов; добавлены ASR-веса/экспорты.
- `test_botmoizvonki_sources.py`: тест конфигурации и интеграционные проверки.

Новые файлы:

- `call_transcription/{__init__,__main__,config,errors,models,audio,diarization,
  processing,roles,transcriber,telegram,cli,worker}.py`;
- `call_transcription/asr/{__init__,base,mlx_backend,faster_whisper_backend}.py`;
- `requirements-transcription-mac.txt`, `requirements-transcription-linux.txt`;
- `scripts/download_transcription_models.py`, `scripts/benchmark_transcription.py`;
- `test_call_transcription.py`, этот README.

Основной `requirements.txt` и обычный Dockerfile не раздуваются PyTorch/Whisper.
В Dockerfile уже установлен ffmpeg. У остальных API и ботов зависимости прежние.

## Установка и скачивание моделей

Python 3.11; создайте отдельное окружение, чтобы не менять зависимости ботов:

```sh
python3.11 -m venv .venv-transcription
.venv-transcription/bin/python -m pip install -r requirements-transcription-mac.txt
# Linux/CPU/CUDA: вместо предыдущего файла requirements-transcription-linux.txt
```

Зачем эти зависимости:

- `mlx-whisper==0.4.3`: Whisper/Metal на Apple Silicon, модель кешируется MLX;
- `faster-whisper==1.2.1`: Whisper/CTranslate2 на CPU/CUDA, также содержит Silero VAD;
- `Abduqayum/whisper-uzbek-medium-callcenter`: узбекский Whisper Medium,
  дообученный на узбекской речи с телефонным диапазоном, шумом и тишиной;
- `pyannote.audio==4.0.7`: локальные голосовые интервалы и VAD; подтягивает PyTorch/numpy;
- ffmpeg + ffprobe: системное декодирование mp3/m4a/wav/ogg/opus в PCM16 mono 16 kHz.

Не ставьте оба ASR-backend без необходимости. CUDA требует совместимых
драйверов/CUDA/cuDNN; на CPU используется int8, на CUDA по умолчанию float16.
На Mac pyannote работает на CPU, Whisper — на Metal. Скорость надо измерять,
а не предполагать по рекламным benchmark.

Для CPU-only VPS есть `requirements-transcription-cpu.txt`: фиксирует CPU
PyTorch 2.8/torchaudio и совместимый TorchCodec 0.7. На небольшом сервере задайте
`LOCAL_DIARIZATION_BATCH_SIZE=1` (по умолчанию 32): это уменьшает промежуточные
тензоры, не заменяет модели. `LOCAL_TRANSCRIPTION_CPU_THREADS=1` ограничивает
потоки. Эти параметры не гарантируют, что весь стек поместится в 4 ГБ вместе
с другими проектами; проверяйте ресурсно ограниченным реальным тестом.

Первоначальное скачивание требует интернета. Примите условия модели
[pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
в своём HF-аккаунте, настройте локальные HF credentials или `HF_TOKEN`.
Токен не помещайте в Git, CLI-аргументы, логи или сообщения.

```sh
.venv-transcription/bin/python scripts/download_transcription_models.py \
  --backend mlx --model large-v3-turbo --directory /absolute/path/models
# Production Linux hybrid: сначала --backend hybrid --model small, затем:
.venv-transcription/bin/python scripts/prepare_uzbek_callcenter_model.py \
  --output-dir /absolute/path/models/whisper-uzbek-callcenter-medium
```

Скачивание — отдельный явный шаг, не часть обработки звонка. Не принимайте
условия моделей автоматически от имени другого человека. Скрипт не обрабатывает
аудио. Затем настройте:

```sh
export LOCAL_WHISPER_MODEL_PATH=/absolute/path/models/whisper
export LOCAL_UZBEK_WHISPER_MODEL=Abduqayum/whisper-uzbek-medium-callcenter
export LOCAL_UZBEK_WHISPER_MODEL_PATH=/absolute/path/models/whisper-uzbek-callcenter-medium
export LOCAL_DIARIZATION_MODEL_PATH=/absolute/path/models/diarization
export LOCAL_WHISPER_MODEL=small
export LOCAL_TRANSCRIPTION_BACKEND=hybrid
```

После скачивания отключите интернет и проверьте CLI. Runtime принимает только
локальные каталоги, включает HF offline и отключает telemetry pyannote/HF.
При недостающих весах он должен завершиться ошибкой, не использовать облачный
fallback и не скачивать веса во время обработки клиента.

Исходный Uzbek checkpoint занимает около 3 ГБ. Он нужен только при явной
подготовке модели. Скрипт фиксирует revision, загружает веса с уменьшенным
потреблением CPU RAM и сохраняет CTranslate2/int8. Production-worker не
импортирует Transformers, не хранит исходные F32-веса и не обращается в сеть
за моделями. Для конвертации установите отдельно
`requirements-transcription-convert.txt`; результат обязательно проверьте
ресурсно ограниченным probe до включения очереди.

## Python API

```python
from call_transcription import CallTranscriber, TranscriptionConfig

transcriber = CallTranscriber(TranscriptionConfig.from_env())  # Один экземпляр на worker.
result = transcriber.transcribe(
    "/calls/call_12345.mp3",
    context_terms=["Samsung Galaxy S25 Ultra", "iPhone 17 Pro Max"],
    call_id=12345,
)
print(result.dialogue)  # Только явный вывод пользователем, не обычный лог worker.
result.save_json("call_12345.json")
result.save_txt("call_12345.txt")
```

Для существующей БД можно передать `context_terms_provider(call_id) -> list[str]`
в конструктор `CallTranscriber`. Модуль не знает схему каталога и не подключается
к Google Sheets/внешней БД. Другой сервис сам читает свой локальный каталог и
передаёт термины. Список брендов служит подсказкой, а не полным каталогом.

```sh
python -m call_transcription /calls/call_12345.mp3
python -m call_transcription /calls/call_12345.mp3 --json output.json --txt output.txt
python scripts/benchmark_transcription.py /calls/call_12345.mp3 --json benchmark.json
```

Benchmark печатает длительность, время работы, realtime factor, backend,
модель, число говорящих/реплик. Первый вызов включает загрузку весов.
Для устойчивой нагрузки повторно используйте тот же экземпляр сервиса.

## Диалог и качество

1. Нормализация во временный PCM WAV; оригинал не изменяется.
2. Pyannote выделяет речевые интервалы, по умолчанию для двух голосов.
3. Близкие речевые интервалы объединяются в окна не длиннее 20 секунд;
   длинные паузы не передаются Whisper. Смена голоса не требует отдельного
   запуска Whisper: слова всё равно сопоставляются с исходными голосовыми
   интервалами по timestamps. `LOCAL_TRANSCRIPTION_SPEECH_GAP` задаёт допустимую
   короткую паузу внутри окна (по умолчанию 0.8 с; на VPS — 1.5 с).
4. Whisper вызывается с `task="transcribe"` и word timestamps. Гибрид явно
   запускает русский и узбекский проходы и выбирает результат по коротким
   временным срезам; MLX определяет язык заново на каждом речевом окне, а не
   один на весь звонок.
   Узбекский Callcenter checkpoint запускается без общего каталожного prompt:
   реальный probe показал, что длинный смешанный prompt провоцирует у него
   повторы приветствий/брендов; названия сохраняются через фактическую речь и
   безопасный post-processing, а не через навязывание текста модели.
5. Word timestamps переводятся в абсолютные секунды и сопоставляются с
   перекрытием голосовых интервалов. Одинаковые голоса объединяются при gap <=0.8 s.
6. Лексический detector выдаёт только `ru`, `uz`, `mixed`, `unknown`.
   Бренды и числа сами по себе не означают английский язык.
7. RoleResolver учитывает характерные сервисные фразы. Одного «Здравствуйте»,
   «Assalomu alaykum», «bor», «цена» недостаточно. При недостатке оснований —
   `role=null`, `speaker_1`/`speaker_2`, а не случайный manager/client.

Whisper даже с transcribe может ошибаться в малоресурсном узбекском,
code-switching, именах и ценах. Prompt не гарантирует точность. MVP не переводит
и не переписывает результат принудительно. Классификатор RU/UZ — лёгкая
эвристика, не обученная языковая модель. ASR confidence — оценка модели/слов,
а role_confidence — эвристический score, **не калиброванная вероятность**.

Одновременная речь не разделяется физически: спорные слова получают
`SPEAKER_UNKNOWN`, `overlap=true`, предупреждение в JSON. Если реально распознан
один голос, второй не выдумывается. Для проверки overlap нужна ручная разметка.

`ProductNameNormalizer` — только необязательный точный alias->каталог utility.
По умолчанию выключен/не применяется: никаких fuzzy-исправлений чисел или
создания несуществующих товаров. Интерфейс `VoiceprintMatcher` предусмотрен,
но биометрическое создание/хранение voiceprints в MVP не реализовано.
Усиленное шумоподавление тоже не включено: оно может уничтожать тихую речь.

## Telegram и БД

Сначала отправляется прежняя карточка. По завершении ASR редактируется **то же**
сообщение, цитата располагается после менеджера/описания звонка. Кнопки,
беззвучность, менеджеры, продажи, SMS, dashboard и webhook-dedup сохраняются.
Для voice используется editMessageCaption, для text — editMessageText.

Подписи voice ограничены 1024 символами, текст — 4096. Expandable blockquote
не отменяет лимит. Длинный диалог явно обозначается как фрагмент со ссылкой на
полный TXT. HTML-символы речи экранируются; длина считается консервативно UTF-16.

`call_transcriptions.transcript_json` хранит полный машинный объект, а
`transcript_txt` — диалог с ролями и временем. Старый `transcript_text` остаётся
для существующего определения источника клиента. Другие поля/таблицы не удалены.
В той же таблице — lease и retry для Telegram-редактирования. Перезапуск или
ошибка Telegram не приводит к повторной ASR/новому посту. Если ASR закончилась
раньше первого Telegram-send, редактирование дождётся message_id. После 10
неудачных edit-попыток ошибка остаётся видимой в таблице для администратора.

Полные результаты доступны в прежнем «Все данные» для звонков с оценкой, а
независимо от оценки — через существующую защиту `/stats/*`:

- `/stats/transcription/12345` — JSON;
- `/stats/transcription/12345?format=txt` — TXT.

В production обязательно оставить `MONITORING_ENABLED=true`. Как и другие
старые `/stats`-маршруты, при отключении общего middleware эти URL не защищены.
Новые публичные ссылки на разговоры и секреты в ссылках не создаются.

Нет речи/нет записи/внутренний контакт — нельзя выдумать диалог. Для звонков без
записи цитата не появится. Посты за всю историю автоматически не пересчитываются.

## Worker и безопасное включение

Предпочтительно запускать тяжёлую модель отдельно от FastAPI, на **том же хосте
и локальном SQLite-томе**, с одним ASR worker. Не используйте сетевую SQLite
и не копируйте работающую БД между компьютерами для синхронизации очереди.
Для удалённого GPU/Mac потребуется отдельно спроектировать защищённую доставку
jobs/results; это намеренно не добавлено в MVP.

В окружении web:

```dotenv
TRANSCRIPTION_ENABLED=true
TRANSCRIPTION_WORKER_MODE=external
MONITORING_ENABLED=true
```

В отдельном worker-окружении должны быть обычные зависимости проекта плюс
один файл `requirements-transcription-*.txt`, тот же DB_PATH, Telegram config
и каталоги локальных моделей:

```sh
python -m call_transcription.worker
```

Возможен `TRANSCRIPTION_WORKER_MODE=embedded` на достаточно мощном хосте;
он использует прежний asyncio/to_thread worker. Не запускайте множество web
реплик с тяжёлыми моделями: кеширование действует на процесс, а не на сервер.
Не переключайте старый облачный API назад: его вызов удалён, старые ASR ключи
теперь не используются. OpenAI/Google/другие облачные ASR не вызываются.

По умолчанию все загруженные для ASR оригиналы и временные WAV удаляются после
задачи, включая ошибки. Caller-owned входной файл CLI не удаляется.
`LOCAL_TRANSCRIPTION_ARCHIVE_DIR` — явный opt-in архив оригиналов; без него
копии не сохраняются. JSON/TXT exports имеют права 0600. Срок хранения уже
существующих записей у «МоиЗвонки» и в Telegram этот модуль не меняет.
В логах — call_id, времена, backend/model, число реплик, confidence, тип ошибки;
нет полного текста. Только разрешённая пользователем интеграция Telegram
публикует диалог в существующий канал звонков; модель не передаёт его в LLM.

## Проверка

```sh
python -m unittest -b test_call_transcription test_botmoizvonki_sources test_monitoring_auth
python -m unittest discover -b
```

Перед production: реальный RU/UZ звонок -> offline CLI -> ручное сравнение с
оригиналом -> измерение RAM/времени -> тестовый Telegram-чат -> включение очереди.
Обязательно проверить тишину, односложные ответы, обе стороны разговора,
перекрытия, цены и названия моделей. Не считать mock-тест принятым benchmark.

## Пример JSON (иллюстрация, не распознанный реальный звонок)

```json
{
  "audio_path": "call:12345",
  "duration": 8.0,
  "speakers": {
    "SPEAKER_00": {"role": "manager", "role_confidence": 0.91, "label": "speaker_1"},
    "SPEAKER_01": {"role": "client", "role_confidence": 0.91, "label": "speaker_2"}
  },
  "segments": [
    {"start": 0.5, "end": 3.0, "speaker_id": "SPEAKER_00", "role": "manager", "language": "ru", "text": "Здравствуйте, магазин Texnikach. Слушаю вас.", "confidence": 0.9, "overlap": false},
    {"start": 4.0, "end": 7.0, "speaker_id": "SPEAKER_01", "role": "client", "language": "uz", "text": "Samsung S25 Ultra bormi?", "confidence": 0.9, "overlap": false}
  ],
  "dialogue": [
    {"role": "manager", "text": "Здравствуйте, магазин Texnikach. Слушаю вас."},
    {"role": "client", "text": "Samsung S25 Ultra bormi?"}
  ]
}
```

Официальные источники реализации:
[faster-whisper](https://github.com/SYSTRAN/faster-whisper),
[MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper),
[pyannote offline](https://huggingface.co/pyannote/speaker-diarization-community-1),
[Telegram formatting](https://core.telegram.org/bots/api#formatting-options).
