import sqlite3
import tempfile
import unittest
from pathlib import Path

from reviews.catalog import CATEGORIES, REASONS, reason_exists
from reviews.database import init_reviews_db
from reviews.service import ReviewValidationError, create_review, normalize_phone


class ReviewsFoundationTests(unittest.TestCase):
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
