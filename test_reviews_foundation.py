import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reviews.catalog import CATEGORIES, REASONS, reason_exists
from reviews.database import init_reviews_db
from reviews.analytics import get_reviews_dashboard
from reviews.router import router as reviews_router
from reviews.service import (
    ReviewValidationError,
    create_review,
    normalize_phone,
    send_review_notification,
)


class ReviewsFoundationTests(unittest.TestCase):
    def test_call_link_opens_system_phone_handler(self):
        app = FastAPI()
        app.include_router(reviews_router)
        client = TestClient(app)
        response = client.get("/call/998901333999")
        self.assertEqual(response.status_code, 200)
        self.assertIn("tel:+998901333999", response.text)
        self.assertEqual(client.get("/call/123").status_code, 404)

    def test_catalog_has_all_categories_and_translations(self):
        expected = {
            "manager", "price", "availability", "delivery",
            "courier", "product", "overall",
        }
        self.assertEqual(set(CATEGORIES), expected)
        self.assertEqual(set(REASONS), expected)
        self.assertTrue(
            all(item["ru"] and item["uz"] for item in CATEGORIES.values())
        )
        self.assertTrue(reason_exists("manager", "slow_response"))
        self.assertFalse(reason_exists("manager", "missing"))

    def test_database_schema_and_seed_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.db"
            init_reviews_db(db_path)

            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                managers = connection.execute(
                    "SELECT code, name FROM managers ORDER BY sort_order"
                ).fetchall()
                review_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(reviews)")
                }

        self.assertTrue({
            "managers", "reviews", "review_scores",
            "review_reason_selections", "review_managers",
        }.issubset(tables))
        self.assertEqual(
            managers,
            [
                ("olmas", "Olmas"),
                ("otabek", "Otabek"),
                ("muhammadali", "MuhammadAli"),
                ("abbos", "Abbos"),
            ],
        )
        self.assertTrue({
            "device_data_json", "request_headers_json", "should_notify",
            "notification_status", "notified_at",
        }.issubset(review_columns))

    def test_review_is_saved_in_normalized_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.db"
            result = create_review(
                {
                    "language": "ru",
                    "source": "qr",
                    "scores": {
                        "manager": {"rating": 2, "comment": "Долго ждал"},
                        "overall": {"rating": 4},
                    },
                    "reasons": {"manager": ["slow_response", "slow_response"]},
                    "managers": ["olmas", "unknown"],
                    "customer_phone": "90 123 45 67",
                },
                ip_address="192.0.2.1",
                user_agent="test",
                db_path=db_path,
            )
            with sqlite3.connect(db_path) as connection:
                review = connection.execute(
                    "SELECT source, customer_phone, needs_attention FROM reviews"
                ).fetchone()
                score_count = connection.execute(
                    "SELECT COUNT(*) FROM review_scores"
                ).fetchone()[0]
                reason_count = connection.execute(
                    "SELECT COUNT(*) FROM review_reason_selections"
                ).fetchone()[0]
                manager_count = connection.execute(
                    "SELECT COUNT(*) FROM review_managers"
                ).fetchone()[0]

        self.assertEqual(result["id"], 1)
        self.assertEqual(review, ("qr", "+998901234567", 1))
        self.assertEqual(score_count, 2)
        self.assertEqual(reason_count, 1)
        self.assertEqual(manager_count, 2)

    def test_existing_reviews_database_is_migrated_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE reviews (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        language TEXT NOT NULL,
                        final_comment TEXT,
                        customer_phone TEXT,
                        ip_hash TEXT,
                        user_agent TEXT,
                        source TEXT NOT NULL,
                        is_delivery_used INTEGER,
                        needs_attention INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO reviews (created_at, language, source)
                    VALUES ('2026-08-21T10:00:00+00:00', 'ru', 'website')
                    """
                )
            init_reviews_db(db_path)
            with sqlite3.connect(db_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(reviews)")
                }
        self.assertEqual(count, 1)
        self.assertIn("device_data_json", columns)
        self.assertIn("notification_status", columns)

    def test_device_data_and_notification_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.db"
            five_only = create_review(
                {"scores": {"overall": {"rating": 5}}},
                ip_address=None, user_agent="UA", db_path=db_path,
            )
            with_comment = create_review(
                {
                    "scores": {"overall": {"rating": 5}},
                    "final_comment": "Спасибо",
                    "device": {
                        "platform": "MacIntel", "screen_width": 1440,
                        "unapproved": "ignored",
                    },
                },
                ip_address=None,
                user_agent="UA",
                accept_language="ru",
                referer="https://example.test/",
                request_headers={"sec-ch-ua-platform": "macOS"},
                db_path=db_path,
            )
            below_five = create_review(
                {"scores": {"price": {"rating": 4}}},
                ip_address=None, user_agent="UA", db_path=db_path,
            )
            dashboard = get_reviews_dashboard(
                {"period": "all", "include_test": "1"}, db_path
            )

        self.assertFalse(five_only["should_notify"])
        self.assertTrue(with_comment["should_notify"])
        self.assertTrue(below_five["should_notify"])
        saved = next(r for r in dashboard["reviews"] if r["id"] == with_comment["id"])
        self.assertEqual(saved["device"]["platform"], "MacIntel")
        self.assertNotIn("unapproved", saved["device"])

    def test_notification_status_is_saved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.db"
            review = create_review(
                {"scores": {"overall": {"rating": 4}}},
                ip_address=None, user_agent=None, db_path=db_path,
            )
            response = Mock()
            response.raise_for_status.return_value = None
            with (
                patch("reviews.service.REVIEWS_TELEGRAM_BOT_TOKEN", "token"),
                patch("reviews.service.REVIEWS_TELEGRAM_CHAT_ID", "-1001"),
                patch("reviews.service.requests.post", return_value=response),
            ):
                self.assertTrue(send_review_notification(review, db_path))
            with sqlite3.connect(db_path) as connection:
                status = connection.execute(
                    "SELECT notification_status FROM reviews WHERE id = ?",
                    (review["id"],),
                ).fetchone()[0]
        self.assertEqual(status, "sent")

    def test_admin_requires_basic_auth(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = FastAPI()
            app.include_router(reviews_router)
            with (
                patch("reviews.router.REVIEWS_ADMIN_PASSWORD", "secret"),
                patch("reviews.router.REVIEWS_DB_PATH", Path(temp_dir) / "reviews.db"),
            ):
                client = TestClient(app)
                self.assertEqual(client.get("/admin/reviews").status_code, 401)
                self.assertEqual(
                    client.get(
                        "/admin/reviews", auth=("admin", "secret")
                    ).status_code,
                    200,
                )
                stats = client.get(
                    "/api/admin/reviews/stats?period=all",
                    auth=("admin", "secret"),
                )
                self.assertEqual(stats.status_code, 200)
                self.assertEqual(stats.json()["summary"]["total"], 0)

    def test_invalid_and_empty_reviews_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.db"
            with self.assertRaises(ReviewValidationError):
                create_review(
                    {}, ip_address=None, user_agent=None, db_path=db_path
                )
            with self.assertRaises(ReviewValidationError):
                create_review(
                    {
                        "scores": {"price": {"rating": 5}},
                        "reasons": {"price": ["price_changed"]},
                    },
                    ip_address=None,
                    user_agent=None,
                    db_path=db_path,
                )

    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("998901234567"), "+998901234567")
        self.assertEqual(normalize_phone("+998 90 123 45 67"), "+998901234567")
        self.assertEqual(normalize_phone("90 123 45 67"), "+998901234567")
        self.assertEqual(normalize_phone("короткий номер"), "короткий номер")


if __name__ == "__main__":
    unittest.main()
