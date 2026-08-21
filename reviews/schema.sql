PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS managers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    language TEXT NOT NULL DEFAULT 'ru' CHECK (language IN ('ru', 'uz')),
    final_comment TEXT,
    customer_phone TEXT,
    ip_hash TEXT,
    user_agent TEXT,
    source TEXT NOT NULL DEFAULT 'website',
    is_delivery_used INTEGER CHECK (is_delivery_used IN (0, 1)),
    needs_attention INTEGER NOT NULL DEFAULT 0 CHECK (needs_attention IN (0, 1))
);

CREATE TABLE IF NOT EXISTS review_scores (
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (
        category IN ('manager', 'price', 'availability', 'delivery', 'courier', 'product', 'overall')
    ),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT,
    PRIMARY KEY (review_id, category)
);

CREATE TABLE IF NOT EXISTS review_reason_selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (review_id, category, reason_code)
);

CREATE TABLE IF NOT EXISTS review_managers (
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    manager_id INTEGER REFERENCES managers(id) ON DELETE RESTRICT,
    selection_type TEXT NOT NULL DEFAULT 'manager' CHECK (
        selection_type IN ('manager', 'other', 'unknown')
    ),
    CHECK (
        (selection_type = 'manager' AND manager_id IS NOT NULL)
        OR (selection_type != 'manager' AND manager_id IS NULL)
    ),
    UNIQUE (review_id, manager_id, selection_type)
);

CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews(created_at);
CREATE INDEX IF NOT EXISTS idx_reviews_attention ON reviews(needs_attention, created_at);
CREATE INDEX IF NOT EXISTS idx_scores_category_rating ON review_scores(category, rating);
CREATE INDEX IF NOT EXISTS idx_reasons_category_code ON review_reason_selections(category, reason_code);
CREATE INDEX IF NOT EXISTS idx_review_managers_manager ON review_managers(manager_id);
