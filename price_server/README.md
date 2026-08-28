# Price server

This package is the server half of the Texnikach price workflow. It is deployed
with the `botmoizvonki` FastAPI application and owns:

- password protection for `/price`;
- durable price snapshots in SQLite;
- Telegram send, edit and delayed-publication jobs;
- a 24-hour delayed-post preview channel with administrator-only cancellation;
- an authoritative day-of-month publication plan at 09:30 Asia/Tashkent;
- automatic updates of links inside the existing Telegram catalogue posts;
- a fenced, durable admin action for updating every current price post;
- a durable Tue/Thu/Sat 11:00 rotation of the pinned catalogue post;
- `chat_id` / `message_id` registry and its `Product Sort` mirror.

Delayed jobs enter the preview channel only when their execution time is no
more than 24 hours away. The bot polls callback and channel-service updates,
removes preview posts after completion/cancellation, and deletes Telegram
service notices such as pin notifications. `PRICE_POST_INDEX_SHEET_NAME`
contains one row for every current price section, including blank IDs for
sections that have never been published.

If Telegram refuses to delete a superseded post (for example, because it is
older than the Bot API deletion window), the bot sends one manual-cleanup link
to the preview channel. An administrator deletes the target post and presses
`Пост удалён`; the bot verifies the target is gone, removes the cleanup link,
and mirrors the final status to Product Sort.

The monthly plan covers days 1 through 30. If the preceding month ends on day
28, the day-29 and day-30 entries are folded into the next month's day 1; if it
ends on day 29, day 30 is folded into day 1. Day 31 has no separate plan. The
SQLite plan is mirrored to `PRICE_CALENDAR_SHEET_NAME`. A successful new
publication supersedes and durably deletes the previous Telegram messages for
the same section.

The existing quick-link catalogue posts are configured in `quick_links.py`.
After a successful new price publication, a separate durable queue edits the
affected catalogue post so its link points to part 1 of the newest publication.
An edit-current action does not change a link. Superseded messages, including
all parts of a multipart publication, remain protected until every affected
catalogue edit succeeds. The quick-post IDs and their applied targets are
mirrored to `PRICE_QUICK_LINKS_SHEET_NAME`; SQLite remains authoritative.
Templates use minimalist Unicode markers (`▸` for a second level and `•` for a
direct link) because Telegram restricts custom-emoji entities for ordinary bots
editing channel posts.

Immediately before a price post is sent or edited, the server applies the
canonical Telegram presentation: both information links point to
`https://texnikach.uz/go`, the section title is visually separated, memory and
price rows use consistent spacing, and the old triangle markers become `↑` and
`↓`. This affects Telegram HTML only; the source snapshot and `/price` page are
not rewritten.

The admin action `Обновить все посты` pins the current snapshot and queues one
sequential edit for each eligible physical Telegram message. Sections that
share one legacy message ID are rendered and persisted together. Every job is
fenced by the exact SQLite binding captured at enqueue time, so it cannot
overwrite a post updated in the meantime. The action never sends or deletes
messages; changed, multipart, or quick-link bindings are skipped and reported.
A durable batch envelope makes the UUID idempotency key effective even when a
request creates zero jobs.

The main catalogue is published every Tuesday, Thursday and Saturday at 11:00
Asia/Tashkent, after the 09:30 price publications. The new catalogue is pinned
and the previous catalogue is recycled into one of eight second-level posts in
this order: smartphones, tablets, audio, wearables, photo/video, VR/glasses,
home/office, charging, then smartphones again. The previous second-level post
is queued for deletion; if Telegram's deletion window has expired, the normal
manual-cleanup link is sent to the preview channel. Quick-post IDs remain
authoritative in SQLite and are mirrored to `PRICE_QUICK_LINKS_SHEET_NAME`.
Every rotation run, including old/new message IDs and pin state, is mirrored to
`PRICE_QUICK_LINK_ROTATIONS_SHEET_NAME`.

Rotation is a forward-only leased state machine. The new `sendMessage` result
is persisted before pin/edit/swap operations continue. The new catalogue keeps
the old second-level URL until the previous main has been successfully recycled;
only then is that one link switched, so every persisted failure phase leaves a
working destination. If a process stops while the send result is unknown, the
run becomes `needs_review` and is never resent blindly. The admin page accepts
either the verified new message ID or an explicit confirmation that no post was
created. Failed idempotent phases can be retried from the same point. Missed,
never-started dates are marked `skipped` instead of being posted in a burst
after downtime. The main post date is stored as publication context, so later
price-link edits do not change it. At 00:01 Asia/Tashkent the scheduler advances
that context to the current local date and queues an idempotent edit of the
active main catalogue. After downtime it catches up once to today's date; a
delayed rotation cannot restore an older date.

The local generator remains in the other project:
`Price2024DB/Cod/Price.py`. It reads local/Google data, builds the price and
sends a signed JSON snapshot to `POST /price/api/v1/sync`. It must not contain
the Telegram bot token.

## Safe rollout

1. Configure a persistent Coolify volume for `/app/data`.
2. Add every `PRICE_*` variable shown in `.env.example`.
3. Deploy this project with `PRICE_SERVER_ENABLED=true`.
4. Test `GET /price/healthz`, then an authenticated `GET /price`.
5. On the Mac set `PRICE_PUBLISH_MODE=both`, `PRICE_SERVER_URL` and the same
   `PRICE_SYNC_API_KEY`; run `Price.py` and compare the new snapshot with the
   legacy page.
6. After parity is confirmed, change the Mac to `PRICE_PUBLISH_MODE=api`.

Do not remove the legacy `/app/price/index.html` mount during the first rollout;
the router uses it as a fallback until the first valid API snapshot exists.
