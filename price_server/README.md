# Price server

This package is the server half of the Texnikach price workflow. It is deployed
with the `botmoizvonki` FastAPI application and owns:

- password protection for `/price`;
- durable price snapshots in SQLite;
- Telegram send, edit and delayed-publication jobs;
- a 24-hour delayed-post preview channel with administrator-only cancellation;
- an authoritative day-of-month publication plan at 09:30 Asia/Tashkent;
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
