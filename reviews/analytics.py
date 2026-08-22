import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .catalog import CATEGORIES, REASONS
from .database import connect_reviews_db, init_reviews_db


UZ_TZ = ZoneInfo("Asia/Tashkent")
TRUE_VALUES = {"1", "true", "yes", "on"}


class AnalyticsFilterError(ValueError):
    pass


def _parse_date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AnalyticsFilterError(f"invalid_{field}") from exc


def _utc_iso(local_date: date) -> str:
    return datetime.combine(local_date, time.min, UZ_TZ).astimezone(timezone.utc).isoformat()


def build_review_filter(params, active_manager_codes: set[str]) -> tuple[str, list, dict]:
    period = str(params.get("period") or "30d")
    if period not in {"today", "yesterday", "7d", "30d", "all", "custom"}:
        raise AnalyticsFilterError("invalid_period")

    where = []
    values: list = []
    today = datetime.now(UZ_TZ).date()
    start_date = end_date = None
    if period == "today":
        start_date, end_date = today, today + timedelta(days=1)
    elif period == "yesterday":
        start_date, end_date = today - timedelta(days=1), today
    elif period == "7d":
        start_date, end_date = today - timedelta(days=6), today + timedelta(days=1)
    elif period == "30d":
        start_date, end_date = today - timedelta(days=29), today + timedelta(days=1)
    elif period == "custom":
        start_date = _parse_date(params.get("date_from"), "date_from")
        selected_end = _parse_date(params.get("date_to"), "date_to")
        if not start_date or not selected_end or selected_end < start_date:
            raise AnalyticsFilterError("invalid_date_range")
        end_date = selected_end + timedelta(days=1)

    if start_date:
        where.append("r.created_at >= ?")
        values.append(_utc_iso(start_date))
    if end_date:
        where.append("r.created_at < ?")
        values.append(_utc_iso(end_date))

    category = str(params.get("category") or "").strip()
    if category and category not in CATEGORIES:
        raise AnalyticsFilterError("invalid_category")

    manager = str(params.get("manager") or "").strip()
    if manager and manager not in active_manager_codes:
        raise AnalyticsFilterError("invalid_manager")
    if manager:
        where.append(
            """
            EXISTS (
                SELECT 1 FROM review_managers rm
                JOIN managers m ON m.id = rm.manager_id
                WHERE rm.review_id = r.id AND m.code = ?
            )
            """
        )
        values.append(manager)

    rating_raw = str(params.get("rating") or "").strip()
    rating = None
    if rating_raw:
        try:
            rating = int(rating_raw)
        except ValueError as exc:
            raise AnalyticsFilterError("invalid_rating") from exc
        if rating not in range(1, 6):
            raise AnalyticsFilterError("invalid_rating")

    if category or rating:
        clauses = ["rs.review_id = r.id"]
        score_values = []
        if category:
            clauses.append("rs.category = ?")
            score_values.append(category)
        if rating:
            clauses.append("rs.rating = ?")
            score_values.append(rating)
        where.append(
            "EXISTS (SELECT 1 FROM review_scores rs WHERE "
            + " AND ".join(clauses)
            + ")"
        )
        values.extend(score_values)

    if str(params.get("attention") or "").lower() in TRUE_VALUES:
        where.append("r.needs_attention = 1")
    if str(params.get("with_comment") or "").lower() in TRUE_VALUES:
        where.append(
            """
            (
                COALESCE(TRIM(r.final_comment), '') != ''
                OR EXISTS (
                    SELECT 1 FROM review_scores rsc
                    WHERE rsc.review_id = r.id
                      AND COALESCE(TRIM(rsc.comment), '') != ''
                )
            )
            """
        )
    if str(params.get("with_phone") or "").lower() in TRUE_VALUES:
        where.append("COALESCE(TRIM(r.customer_phone), '') != ''")
    if str(params.get("include_test") or "").lower() not in TRUE_VALUES:
        where.append("r.source != 'test'")

    source = str(params.get("source") or "").strip()
    if source:
        where.append("r.source = ?")
        values.append(source[:40])

    where_sql = " AND ".join(where) if where else "1 = 1"
    applied = {
        "period": period,
        "date_from": start_date.isoformat() if start_date else None,
        "date_to": (end_date - timedelta(days=1)).isoformat() if end_date else None,
        "category": category or None,
        "manager": manager or None,
        "rating": rating,
    }
    return where_sql, values, applied


def _parse_json(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return {"raw": str(value)}


def _hydrate_reviews(connection, rows) -> list[dict]:
    reviews = [dict(row) for row in rows]
    if not reviews:
        return []
    by_id = {item["id"]: item for item in reviews}
    ids = list(by_id)
    placeholders = ",".join("?" for _ in ids)
    for item in reviews:
        item["scores"] = {}
        item["reasons"] = {}
        item["managers"] = []
        item["device"] = _parse_json(item.pop("device_data_json", None))
        item["request_headers"] = _parse_json(item.pop("request_headers_json", None))
        item["ip_hash_short"] = (item.pop("ip_hash", "") or "")[:12]

    for row in connection.execute(
        f"SELECT review_id, category, rating, comment FROM review_scores WHERE review_id IN ({placeholders})",
        ids,
    ):
        by_id[row["review_id"]]["scores"][row["category"]] = {
            "rating": row["rating"], "comment": row["comment"]
        }
    for row in connection.execute(
        f"SELECT review_id, category, reason_code FROM review_reason_selections WHERE review_id IN ({placeholders})",
        ids,
    ):
        review = by_id[row["review_id"]]
        review["reasons"].setdefault(row["category"], []).append(row["reason_code"])
    for row in connection.execute(
        f"""
        SELECT rm.review_id, rm.selection_type, m.code, m.name
        FROM review_managers rm
        LEFT JOIN managers m ON m.id = rm.manager_id
        WHERE rm.review_id IN ({placeholders})
        """,
        ids,
    ):
        by_id[row["review_id"]]["managers"].append({
            "type": row["selection_type"],
            "code": row["code"],
            "name": row["name"],
        })
    return reviews


def get_reviews_dashboard(params, db_path: Path | str) -> dict:
    init_reviews_db(db_path)
    with connect_reviews_db(db_path) as connection:
        manager_codes = {
            row["code"]
            for row in connection.execute("SELECT code FROM managers WHERE active = 1")
        }
        where_sql, values, applied = build_review_filter(params, manager_codes)
        cte = f"WITH filtered AS (SELECT r.id FROM reviews r WHERE {where_sql})"

        summary_row = connection.execute(
            cte
            + """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(r.needs_attention), 0) AS attention,
                COALESCE(SUM(CASE WHEN COALESCE(TRIM(r.customer_phone), '') != '' THEN 1 ELSE 0 END), 0) AS with_phone,
                COALESCE(SUM(CASE WHEN r.notification_status = 'sent' THEN 1 ELSE 0 END), 0) AS notified,
                COALESCE(SUM(CASE WHEN COALESCE(TRIM(r.final_comment), '') != '' OR EXISTS (
                    SELECT 1 FROM review_scores sc WHERE sc.review_id = r.id AND COALESCE(TRIM(sc.comment), '') != ''
                ) THEN 1 ELSE 0 END), 0) AS with_comment
            FROM reviews r JOIN filtered f ON f.id = r.id
            """,
            values,
        ).fetchone()

        category_rows = connection.execute(
            cte
            + """
            SELECT s.category, COUNT(*) AS count, ROUND(AVG(s.rating), 2) AS average,
                   SUM(CASE WHEN s.rating = 1 THEN 1 ELSE 0 END) AS r1,
                   SUM(CASE WHEN s.rating = 2 THEN 1 ELSE 0 END) AS r2,
                   SUM(CASE WHEN s.rating = 3 THEN 1 ELSE 0 END) AS r3,
                   SUM(CASE WHEN s.rating = 4 THEN 1 ELSE 0 END) AS r4,
                   SUM(CASE WHEN s.rating = 5 THEN 1 ELSE 0 END) AS r5
            FROM review_scores s JOIN filtered f ON f.id = s.review_id
            GROUP BY s.category
            """,
            values,
        ).fetchall()
        category_map = {row["category"]: dict(row) for row in category_rows}
        categories = [
            {
                "code": code,
                "label": CATEGORIES[code]["ru"],
                **category_map.get(code, {
                    "count": 0, "average": None,
                    "r1": 0, "r2": 0, "r3": 0, "r4": 0, "r5": 0,
                }),
            }
            for code in CATEGORIES
        ]
        for item in categories:
            item.pop("category", None)

        manager_rows = connection.execute(
            cte
            + """
            SELECT m.code, m.name, COUNT(s.rating) AS count,
                   ROUND(AVG(s.rating), 2) AS average,
                   SUM(CASE WHEN s.rating = 1 THEN 1 ELSE 0 END) AS r1,
                   SUM(CASE WHEN s.rating = 2 THEN 1 ELSE 0 END) AS r2,
                   SUM(CASE WHEN s.rating = 3 THEN 1 ELSE 0 END) AS r3,
                   SUM(CASE WHEN s.rating = 4 THEN 1 ELSE 0 END) AS r4,
                   SUM(CASE WHEN s.rating = 5 THEN 1 ELSE 0 END) AS r5,
                   ROUND(100.0 * SUM(CASE WHEN s.rating >= 4 THEN 1 ELSE 0 END) / NULLIF(COUNT(s.rating), 0), 1) AS positive_percent
            FROM managers m
            LEFT JOIN review_managers rm ON rm.manager_id = m.id
            LEFT JOIN filtered f ON f.id = rm.review_id
            LEFT JOIN review_scores s ON s.review_id = f.id AND s.category = 'manager'
            WHERE m.active = 1
            GROUP BY m.id ORDER BY m.sort_order, m.name
            """,
            values,
        ).fetchall()

        reason_rows = connection.execute(
            cte
            + """
            SELECT rr.category, rr.reason_code, COUNT(*) AS count
            FROM review_reason_selections rr JOIN filtered f ON f.id = rr.review_id
            GROUP BY rr.category, rr.reason_code
            ORDER BY count DESC, rr.category, rr.reason_code LIMIT 30
            """,
            values,
        ).fetchall()
        reasons = [
            {
                **dict(row),
                "label": REASONS.get(row["category"], {}).get(
                    row["reason_code"], (row["reason_code"], "")
                )[0],
            }
            for row in reason_rows
        ]

        trend_rows = connection.execute(
            cte
            + """
            SELECT strftime('%Y-%m-%d', r.created_at, '+5 hours') AS day,
                   COUNT(DISTINCT r.id) AS count,
                   ROUND(AVG(CASE WHEN s.category = 'overall' THEN s.rating END), 2) AS overall_average
            FROM reviews r JOIN filtered f ON f.id = r.id
            LEFT JOIN review_scores s ON s.review_id = r.id
            GROUP BY day ORDER BY day
            """,
            values,
        ).fetchall()

        recent_rows = connection.execute(
            cte
            + """
            SELECT r.* FROM reviews r JOIN filtered f ON f.id = r.id
            ORDER BY r.created_at DESC LIMIT 100
            """,
            values,
        ).fetchall()
        reviews = _hydrate_reviews(connection, recent_rows)
        managers = [
            dict(row)
            for row in connection.execute(
                "SELECT code, name FROM managers WHERE active = 1 ORDER BY sort_order, name"
            )
        ]
        sources = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT source FROM reviews ORDER BY source"
            )
        ]

    return {
        "summary": dict(summary_row),
        "categories": categories,
        "managers": [dict(row) for row in manager_rows],
        "reason_stats": reasons,
        "trend": [dict(row) for row in trend_rows],
        "reviews": reviews,
        "filter_options": {"managers": managers, "sources": sources},
        "applied": applied,
    }


def get_review_detail(review_id: int, db_path: Path | str) -> dict | None:
    init_reviews_db(db_path)
    with connect_reviews_db(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
        hydrated = _hydrate_reviews(connection, [row] if row else [])
    return hydrated[0] if hydrated else None
