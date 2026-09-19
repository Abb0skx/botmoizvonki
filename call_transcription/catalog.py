"""Read-only catalogue hints from the existing price service, or a JSON file."""
import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)


def load_catalog(path):
    if not path:
        return [], {}
    try:
        source = Path(path).resolve()
        aliases = {}
        if source.suffix in {".db", ".sqlite", ".sqlite3"}:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "price_product_sets" in tables:
                    row = conn.execute("""SELECT p.payload_json FROM price_product_sets p
                        JOIN price_snapshot_storage s ON s.product_set_hash=p.product_set_hash
                        JOIN price_snapshots snapshot ON snapshot.snapshot_id=s.snapshot_id
                        WHERE snapshot.is_current=1 LIMIT 1""").fetchone()
                    products = json.loads(row[0]) if row else []
                else:
                    row = conn.execute("SELECT payload_json FROM price_snapshots WHERE is_current=1 LIMIT 1").fetchone()
                    products = json.loads(row[0]).get("products", []) if row else []
            terms = [p.get("model_name", "") for p in products if isinstance(p, dict)]
        else:
            payload = json.loads(source.read_text(encoding="utf-8"))
            terms = payload.get("terms", [])
            aliases = payload.get("aliases", {})
        terms = sorted({t.strip() for t in terms if isinstance(t, str) and 1 < len(t.strip()) <= 120})
        aliases = {a: t for a, t in aliases.items() if isinstance(a, str) and t in terms}
        return terms, aliases
    except (OSError, ValueError, sqlite3.Error, AttributeError) as exc:
        logger.warning("transcription_catalog_unavailable error=%s", type(exc).__name__)
        return [], {}


def catalogue_aliases(terms, explicit=None):
    """Exact known spelling substitutions only; never fuzzy prices/model numbers."""
    substitutions = {
        "iphone": "айфон", "samsung": "самсунг", "galaxy": "галакси",
        "google": "гугл", "pixel": "пиксель", "redmi": "редми",
        "poco": "поко", "tecno": "текно", "pro": "про", "max": "макс",
        "ultra": "ультра", "note": "ноут",
    }
    aliases = dict(explicit or {})
    numbers = {"1": "один", "2": "два", "3": "три", "4": "четыре", "5": "пять",
               "6": "шесть", "7": "семь", "8": "восемь", "9": "девять", "10": "десять",
               "11": "одиннадцать", "12": "двенадцать", "13": "тринадцать", "14": "четырнадцать",
               "15": "пятнадцать", "16": "шестнадцать", "17": "семнадцать", "18": "восемнадцать"}
    ambiguous = set()
    def register(alias, target):
        if alias in aliases and aliases[alias] != target:
            ambiguous.add(alias)
        else:
            aliases[alias] = target
    for term in terms:
        variant = re.sub(r"[A-Za-z]+", lambda m: substitutions.get(m[0].lower(), m[0]), term)
        if variant != term:
            register(variant, term)
            spoken = re.sub(r"\b(\d{1,2})([Aa])?\b", lambda m: numbers.get(m[1], m[1]) + (" а" if m[2] else ""), variant)
            if spoken != variant:
                register(spoken, term)
    return {a: t for a, t in aliases.items() if a not in ambiguous}
