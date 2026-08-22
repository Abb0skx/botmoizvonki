import os
import secrets
from pathlib import Path


REVIEWS_DB_PATH = Path(
    os.getenv("REVIEWS_DB_PATH", "/app/data/reviews.db")
)

REVIEWS_COMPLAINT_PHONE = os.getenv(
    "REVIEWS_COMPLAINT_PHONE", "+998901333999"
).strip()
REVIEWS_COMPLAINT_TELEGRAM = os.getenv(
    "REVIEWS_COMPLAINT_TELEGRAM", "https://t.me/AbbosTch"
).strip()
REVIEWS_TELEGRAM_BOT_TOKEN = os.getenv(
    "REVIEWS_TELEGRAM_BOT_TOKEN", ""
).strip()
REVIEWS_TELEGRAM_CHAT_ID = os.getenv(
    "REVIEWS_TELEGRAM_CHAT_ID", ""
).strip()
REVIEWS_SITE_URL = os.getenv(
    "REVIEWS_SITE_URL", "https://texnikach.uz"
).rstrip("/")
REVIEWS_INSTAGRAM_URL = os.getenv("REVIEWS_INSTAGRAM_URL", "").strip()
REVIEWS_TELEGRAM_URL = os.getenv("REVIEWS_TELEGRAM_URL", "").strip()
REVIEWS_ADMIN_USERNAME = os.getenv(
    "REVIEWS_ADMIN_USERNAME", "admin"
).strip() or "admin"
REVIEWS_ADMIN_PASSWORD = os.getenv(
    "REVIEWS_ADMIN_PASSWORD", ""
)
REVIEWS_IP_HASH_SECRET = (
    os.getenv("REVIEWS_IP_HASH_SECRET", "").strip()
    or secrets.token_hex(32)
)

try:
    REVIEWS_CRITICAL_RATING = min(
        5, max(1, int(os.getenv("REVIEWS_CRITICAL_RATING", "2")))
    )
except ValueError:
    REVIEWS_CRITICAL_RATING = 2

try:
    REVIEWS_RATE_LIMIT = max(
        1, int(os.getenv("REVIEWS_RATE_LIMIT", "5"))
    )
except ValueError:
    REVIEWS_RATE_LIMIT = 5

try:
    REVIEWS_RATE_WINDOW_SECONDS = max(
        60, int(os.getenv("REVIEWS_RATE_WINDOW_SECONDS", "900"))
    )
except ValueError:
    REVIEWS_RATE_WINDOW_SECONDS = 900
