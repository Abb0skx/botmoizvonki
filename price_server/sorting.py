"""Authoritative, versioned catalogue ordering; never modifies prices or products.

The initial import retains the old spreadsheet's *exact* rank dictionaries.
Independent lists are deliberately not joined by spreadsheet row number.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .price_entry import EntryError, now


ORDER_NAMES = ("category_order", "supplier_category_order", "model_order",
               "memory_order", "color_order", "raw_color_order")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(prefix, value):
    return prefix + ":" + _digest(value)[:24]


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise EntryError("invalid_sorting_seed")
    return value.strip()


def _heading(name):
    return name.startswith(("▸", "— "))


def _order(values):
    return {value: index for index, value in enumerate(dict.fromkeys(values))}


def _tables(db):
    statements = (
        """CREATE TABLE IF NOT EXISTS price_sorting_state(
            id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL, seed_hash TEXT NOT NULL,
            catalog_groups_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS price_sorting_groups(
            group_id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
            position INTEGER NOT NULL, category_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS price_sorting_items(
            item_id TEXT PRIMARY KEY, group_id TEXT NOT NULL
                REFERENCES price_sorting_groups(group_id),
            name TEXT NOT NULL, canonical TEXT, position INTEGER NOT NULL,
            is_new INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            category_id INTEGER)""",
        """CREATE INDEX IF NOT EXISTS price_sorting_items_group
            ON price_sorting_items(group_id,position,item_id)""",
        """CREATE TABLE IF NOT EXISTS price_sorting_orders(
            dimension TEXT NOT NULL, name TEXT NOT NULL, rank INTEGER NOT NULL,
            PRIMARY KEY(dimension,name))""",
        """CREATE TABLE IF NOT EXISTS price_sorting_aliases(
            raw_name TEXT PRIMARY KEY, canonical TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS price_sorting_operations(
            operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
            created_at TEXT NOT NULL, result_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS price_sorting_history(
            revision INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
            action TEXT NOT NULL, summary TEXT NOT NULL, operation_id TEXT,
            changes_json TEXT NOT NULL)""",
    )
    for statement in statements:
        db.execute(statement)


@contextmanager
def _connection(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        _tables(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _require(db):
    state = db.execute("SELECT * FROM price_sorting_state WHERE id=1").fetchone()
    if not state:
        raise EntryError("sorting_not_initialized", 503)
    return state


def _catalog(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_products'").fetchone():
        raise EntryError("local_prices_not_initialized", 503)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_categories'").fetchone():
        raise EntryError("catalog_management_not_initialized", 503)
    categories = {int(row[0]): str(row[1]) for row in db.execute(
        "SELECT category_id,name FROM entry_categories ORDER BY category_id")}
    models, memories, colors = set(), set(), set()
    for row in db.execute("SELECT metadata_json FROM entry_products ORDER BY position"):
        try:
            product = json.loads(row[0])
            category_id = int(product["category_id"])
            name = _name(product["model_name"])
            if category_id not in categories:
                raise ValueError()
            models.add((category_id, name))
            if str(product.get("memory") or "").strip():
                memories.add(str(product["memory"]).strip())
            if str(product.get("color") or "").strip():
                colors.add(str(product["color"]).strip())
        except (ValueError, TypeError, KeyError):
            raise EntryError("invalid_catalog_product", 409) from None
    return categories, models, memories, colors


def _group(db, group_id, title, kind, position=0, category_id=None):
    db.execute("""INSERT OR IGNORE INTO price_sorting_groups
        (group_id,title,kind,position,category_id) VALUES (?,?,?,?,?)""",
               (group_id, title, kind, position, category_id))


def _rank(db, dimension, name):
    row = db.execute("SELECT rank FROM price_sorting_orders WHERE dimension=? AND name=?",
                     (dimension, name)).fetchone()
    return None if row is None else int(row[0])


def _append_rank(db, dimension, name):
    rank = _rank(db, dimension, name)
    if rank is None:
        rank = db.execute("SELECT coalesce(max(rank),-1)+1 FROM price_sorting_orders "
                          "WHERE dimension=?", (dimension,)).fetchone()[0]
        db.execute("INSERT INTO price_sorting_orders VALUES (?,?,?)", (dimension, name, rank))
    return rank


def _add_item(db, item_id, group_id, name, *, canonical=None, is_new=False,
              active=True, category_id=None):
    position = db.execute("SELECT coalesce(max(position),-1)+1 FROM price_sorting_items "
                          "WHERE group_id=?", (group_id,)).fetchone()[0]
    db.execute("""INSERT INTO price_sorting_items
        (item_id,group_id,name,canonical,position,is_new,active,category_id)
        VALUES (?,?,?,?,?,?,?,?)""",
               (item_id, group_id, name, canonical, position, int(is_new), int(active), category_id))


def _memory_group(name):
    value = name.lower().replace(" ", "")
    if re.search(r"\d\s*(mm|мм|inch|дюйм)", value):
        return "memories:sizes", "Размеры"
    if re.search(r"\d+[/.]\d+.*(gb|гб|tb|тб)", value):
        return "memories:ram-storage", "Оперативная память / накопитель"
    if re.search(r"\d.*(gb|гб|tb|тб|mb|мб)", value):
        return "memories:storage", "Объём памяти"
    return "memories:other", "Другие конфигурации"


def _color_group(db, name):
    # Prefer the existing family's title. Unknown branded shades are never guessed.
    families = (
        (r"\b(black|ч[её]рн\w*)\b", "Чёрные"),
        (r"\b(white|бел\w*)\b", "Белые"),
        (r"\b(gray|grey|сер\w*)\b", "Серые"),
        (r"\b(blue|син\w*|голуб\w*)\b", "Синие и голубые"),
        (r"\b(green|зел[её]н\w*)\b", "Зелёные"),
        (r"\b(purple|violet|фиолет\w*)\b", "Фиолетовые"),
        (r"\b(red|burgundy|красн\w*|бордов\w*)\b", "Красные и бордовые"),
        (r"\b(pink|розов\w*)\b", "Розовые"),
        (r"\b(gold|золот\w*)\b", "Золотые"),
        (r"\b(silver|серебрист\w*)\b", "Серебристые"),
        (r"\b(orange|coral|оранж\w*|коралл\w*)\b", "Оранжевые и коралловые"),
        (r"\b(brown|bronze|коричнев\w*|бронз\w*)\b", "Коричневые и бронзовые"),
        (r"\b(yellow|ж[её]лт\w*)\b", "Жёлтые"),
    )
    matches = [title for pattern, title in families if re.search(pattern, name, re.I)]
    title = matches[0] if len(matches) == 1 else "Другие цвета"
    existing = db.execute("SELECT group_id,title FROM price_sorting_groups WHERE kind='colors' "
                          "AND title=?", (title,)).fetchone()
    return (existing[0], existing[1]) if existing else (_id("colors", title), title)


def _history(db, action, summary, changes, operation_id=None):
    revision = int(_require(db)["revision"]) + 1
    timestamp = now()
    db.execute("UPDATE price_sorting_state SET revision=?,updated_at=? WHERE id=1",
               (revision, timestamp))
    db.execute("INSERT INTO price_sorting_history VALUES (?,?,?,?,?,?)",
               (revision, timestamp, action, summary, operation_id, _json(changes)))


def _discover(db):
    categories, models, memories, colors = _catalog(db)
    items = {row["item_id"]: dict(row) for row in db.execute("SELECT * FROM price_sorting_items")}
    changes = {"added": [], "renamed": [], "activity": []}
    for category_id, name in categories.items():
        group_id = f"models:{category_id}"
        order_rank = _append_rank(db, "category_order", name)
        _group(db, group_id, name, "models", order_rank, category_id)
        db.execute("UPDATE price_sorting_groups SET title=?,position=? WHERE group_id=?",
                   (name, order_rank, group_id))
        for suffix, dimension, category_group in (
                ("price", "category_order", "categories:price"),
                ("suppliers", "supplier_category_order", "categories:suppliers")):
            item_id = f"category:{suffix}:{category_id}"
            previous = items.get(item_id)
            _append_rank(db, dimension, name)
            if not previous:
                _add_item(db, item_id, category_group, name, is_new=True, category_id=category_id)
                changes["added"].append(item_id)
            elif previous["name"] != name:
                old_rank = _rank(db, dimension, previous["name"])
                db.execute("DELETE FROM price_sorting_orders WHERE dimension=? AND name=?",
                           (dimension, previous["name"]))
                db.execute("UPDATE price_sorting_orders SET rank=? WHERE dimension=? AND name=?",
                           (old_rank, dimension, name))
                db.execute("UPDATE price_sorting_items SET name=? WHERE item_id=?", (name, item_id))
                changes["renamed"].append(item_id)
        db.execute("UPDATE price_sorting_groups SET position=? WHERE group_id=?",
                   (_rank(db, "category_order", name), group_id))
    for category_id, name in sorted(models, key=lambda item: (item[0], item[1].casefold(), item[1])):
        item_id = _id("model", [category_id, name])
        if item_id not in items:
            _append_rank(db, "model_order", name)
            _add_item(db, item_id, f"models:{category_id}", name, is_new=True,
                      category_id=category_id)
            changes["added"].append(item_id)
    for name in sorted(memories, key=lambda value: (value.casefold(), value)):
        item_id = _id("memory", name)
        if item_id not in items:
            _append_rank(db, "memory_order", name)
            group_id, title = _memory_group(name)
            _group(db, group_id, title, "memories", 3000)
            _add_item(db, item_id, group_id, name, is_new=True)
            changes["added"].append(item_id)
    for name in sorted(colors, key=lambda value: (value.casefold(), value)):
        item_id = _id("color", name)
        if item_id not in items:
            alias = db.execute("SELECT canonical FROM price_sorting_aliases WHERE raw_name=?",
                               (name.lower(),)).fetchone()
            canonical = alias[0] if alias else name
            db.execute("INSERT OR IGNORE INTO price_sorting_aliases VALUES (?,?)", (name.lower(), canonical))
            _append_rank(db, "color_order", canonical)
            _append_rank(db, "raw_color_order", name)
            group_id, title = _color_group(db, name)
            _group(db, group_id, title, "colors", 2000)
            _add_item(db, item_id, group_id, name, canonical=canonical, is_new=True)
            changes["added"].append(item_id)
    for item in items.values():
        prefix = item["item_id"].split(":", 1)[0]
        if prefix == "model":
            active = (item["category_id"], item["name"]) in models
        elif prefix == "memory":
            active = item["name"] in memories
        elif prefix == "color":
            active = item["name"] in colors
        elif prefix == "category":
            active = item["category_id"] in categories
        else:
            continue
        if bool(item["active"]) != active:
            db.execute("UPDATE price_sorting_items SET active=? WHERE item_id=?", (int(active), item["item_id"]))
            changes["activity"].append(item["item_id"])
    if any(changes.values()):
        _history(db, "discovery", f"Новые позиции: {len(changes['added'])}", changes)


def _state(db):
    state = _require(db)
    groups = []
    for row in db.execute("""SELECT g.*,count(i.item_id) AS count,
            coalesce(sum(i.is_new),0) AS new_count
        FROM price_sorting_groups g LEFT JOIN price_sorting_items i USING(group_id)
        GROUP BY g.group_id ORDER BY g.position,g.group_id"""):
        group = {"id": row["group_id"], "title": row["title"], "kind": row["kind"],
                 "position": row["position"], "count": row["count"], "new_count": row["new_count"]}
        if row["category_id"] is not None:
            group["category_id"] = row["category_id"]
        groups.append(group)
    items = []
    for row in db.execute("SELECT * FROM price_sorting_items ORDER BY group_id,position,item_id"):
        item = {"id": row["item_id"], "group_id": row["group_id"], "name": row["name"],
                "position": row["position"], "is_new": bool(row["is_new"]), "active": bool(row["active"])}
        if row["canonical"] is not None:
            item["canonical"] = row["canonical"]
        items.append(item)
    history = [dict(row) for row in db.execute("""SELECT revision,created_at,action,summary,operation_id
        FROM price_sorting_history ORDER BY revision DESC LIMIT 30""")]
    return {"revision": state["revision"], "updated_at": state["updated_at"],
            "source": "sqlite", "groups": groups, "items": items, "history": history}


def _seed(seed):
    if not isinstance(seed, dict):
        raise EntryError("invalid_sorting_seed")
    result = {}
    for key in ("models", "category_names", "supplier_category_names", "memories"):
        values = seed.get(key, [])
        if not isinstance(values, list) or len(values) > 30000:
            raise EntryError("invalid_sorting_seed")
        result[key] = list(dict.fromkeys(_name(value) for value in values))
    colors = seed.get("colors", [])
    if not isinstance(colors, list) or len(colors) > 30000:
        raise EntryError("invalid_sorting_seed")
    result["colors"] = []
    for color in colors:
        if not isinstance(color, dict):
            raise EntryError("invalid_sorting_seed")
        result["colors"].append({"name": _name(color.get("name")),
            "canonical": _name(color["canonical"]) if color.get("canonical") is not None else None,
            "group": str(color.get("group") or "Другие цвета").removeprefix("▸").strip()})
    orders = {
        "category_order": _order(result["category_names"]),
        "supplier_category_order": _order(result["supplier_category_names"]),
        "model_order": _order(result["models"]), "memory_order": _order(result["memories"]),
        "color_order": _order([item["canonical"] for item in result["colors"] if item["canonical"]]),
        "raw_color_order": _order([item["name"] for item in result["colors"]]),
    }
    provided = seed.get("orders", {})
    if not isinstance(provided, dict):
        raise EntryError("invalid_sorting_seed")
    for dimension in ORDER_NAMES:
        if dimension in provided:
            values = provided[dimension]
            if not isinstance(values, dict) or len(values) > 30000 or any(
                    not isinstance(name, str) or not name.strip() or type(rank) is not int or rank < 0
                    for name, rank in values.items()):
                raise EntryError("invalid_sorting_seed")
            if len(set(values.values())) != len(values):
                raise EntryError("invalid_sorting_seed")
            orders[dimension] = dict(values)
    aliases = {item["name"].lower(): item["canonical"] for item in result["colors"] if item["canonical"]}
    if "color_replace_dict" in provided:
        aliases = provided["color_replace_dict"]
        if not isinstance(aliases, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in aliases.items()):
            raise EntryError("invalid_sorting_seed")
    catalog_groups = seed.get("catalog_groups", [])
    if not isinstance(catalog_groups, list):
        raise EntryError("invalid_sorting_seed")
    result.update(orders=orders, aliases=aliases, catalog_groups=catalog_groups)
    return result


def install_sorting(db_path: Path | str, seed: dict) -> dict:
    """One-time import from a backed-up spreadsheet. Repeating it cannot reset edits."""
    parsed = _seed(seed)
    seed_hash = _digest(parsed)
    with _connection(db_path) as db:
        previous = db.execute("SELECT seed_hash FROM price_sorting_state WHERE id=1").fetchone()
        if previous:
            if previous[0] != seed_hash:
                raise EntryError("sorting_migration_conflict", 409)
            return {"status": "already_initialized", **_state(db)}
        categories, models, memories, colors = _catalog(db)
        db.execute("INSERT INTO price_sorting_state VALUES (1,0,?,?,?)",
                   (now(), seed_hash, _json(parsed["catalog_groups"])))
        for dimension, values in parsed["orders"].items():
            db.executemany("INSERT INTO price_sorting_orders VALUES (?,?,?)",
                           [(dimension, name, rank) for name, rank in values.items()])
        db.executemany("INSERT INTO price_sorting_aliases VALUES (?,?)", parsed["aliases"].items())
        _group(db, "categories:price", "Категории прайса", "categories", -2)
        _group(db, "categories:suppliers", "Категории поставщиков", "categories", -1)
        by_name = {name: category_id for category_id, name in categories.items()}
        for suffix, dimension, category_group in (
                ("price", "category_order", "categories:price"),
                ("suppliers", "supplier_category_order", "categories:suppliers")):
            for name in sorted(parsed["orders"][dimension], key=parsed["orders"][dimension].get):
                if _heading(name):
                    continue
                category_id = by_name.get(name)
                item_id = f"category:{suffix}:{category_id}" if category_id is not None else _id("legacy-category:" + suffix, name)
                _add_item(db, item_id, category_group, name, active=category_id is not None,
                          category_id=category_id)
        for category_id, name in categories.items():
            _group(db, f"models:{category_id}", name, "models",
                   parsed["orders"]["category_order"].get(name, 1000 + category_id), category_id)
        used_names = set()
        for category_id, name in sorted(models, key=lambda item: (
                item[0], parsed["orders"]["model_order"].get(item[1], 999999), item[1])):
            if name in parsed["orders"]["model_order"]:
                _add_item(db, _id("model", [category_id, name]), f"models:{category_id}", name,
                          category_id=category_id)
                used_names.add(name)
        legacy = [name for name in parsed["models"] if name not in used_names and not _heading(name)]
        if legacy:
            _group(db, "models:legacy", "Другие модели из прежней таблицы", "models", 1999)
            for name in legacy:
                _add_item(db, _id("legacy-model", name), "models:legacy", name, active=False)
        for name in sorted(parsed["orders"]["memory_order"], key=parsed["orders"]["memory_order"].get):
            if _heading(name):
                continue
            group_id, title = _memory_group(name)
            _group(db, group_id, title, "memories", 3000)
            _add_item(db, _id("memory", name), group_id, name, active=name in memories)
        seen = set()
        for color in parsed["colors"]:
            name = color["name"]
            if name in seen or _heading(name):
                continue
            seen.add(name)
            group_id = _id("colors", color["group"])
            _group(db, group_id, color["group"], "colors", 2000 + len(seen))
            _add_item(db, _id("color", name), group_id, name,
                      canonical=parsed["aliases"].get(name.lower(), color["canonical"]), active=name in colors)
        _history(db, "migration", "Импорт существующего порядка из Product Sort", {"seed_hash": seed_hash})
        _discover(db)
        return {"status": "initialized", **_state(db)}


class SortingService:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def state(self) -> dict:
        with _connection(self.db_path) as db:
            _require(db)
            _discover(db)
            return _state(db)

    def export(self) -> dict:
        from .accessories import unified_accessories_catalog
        with _connection(self.db_path) as db:
            _require(db)
            _discover(db)
            state = _require(db)
            result = {"schema_version": 1, "source": "sqlite", "revision": state["revision"],
                      "updated_at": state["updated_at"],
                      "catalog_groups": unified_accessories_catalog(json.loads(state["catalog_groups_json"]))}
            for dimension in ORDER_NAMES:
                result[dimension] = {row[0]: row[1] for row in db.execute(
                    "SELECT name,rank FROM price_sorting_orders WHERE dimension=? ORDER BY rank,name",
                    (dimension,))}
            result["color_replace_dict"] = dict(db.execute(
                "SELECT raw_name,canonical FROM price_sorting_aliases ORDER BY raw_name"))
            result["content_hash"] = _digest(result)
            return result

    def save(self, body: dict, operation_id: str) -> dict:
        if not isinstance(body, dict) or set(body) != {"revision", "groups"} \
                or type(body.get("revision")) is not int or body["revision"] < 0 \
                or not isinstance(body.get("groups"), list) or not 1 <= len(body["groups"]) <= 1000:
            raise EntryError("invalid_sorting_request")
        try:
            uuid.UUID(operation_id)
        except (ValueError, AttributeError, TypeError):
            raise EntryError("idempotency_key_required") from None
        request_hash = _digest(body)
        with _connection(self.db_path) as db:
            _require(db)
            replay = db.execute("SELECT request_hash,result_json FROM price_sorting_operations "
                                "WHERE operation_id=?", (operation_id,)).fetchone()
            if replay:
                if replay[0] != request_hash:
                    raise EntryError("idempotency_conflict", 409)
                return json.loads(replay[1])
            _discover(db)
            if body["revision"] != _require(db)["revision"]:
                raise EntryError("sorting_revision_conflict", 409, revision=_require(db)["revision"])
            all_groups = {row["group_id"]: dict(row) for row in db.execute("SELECT * FROM price_sorting_groups")}
            all_items = {row["item_id"]: dict(row) for row in db.execute("SELECT * FROM price_sorting_items")}
            requested, union = {}, []
            for group in body["groups"]:
                if not isinstance(group, dict) or set(group) != {"group_id", "item_ids"}:
                    raise EntryError("invalid_sorting_request")
                group_id, ids = group["group_id"], group["item_ids"]
                if not isinstance(group_id, str) or group_id not in all_groups or group_id in requested \
                        or not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                    raise EntryError("invalid_sorting_group")
                requested[group_id] = ids
                union.extend(ids)
            expected = {i for i, item in all_items.items() if item["group_id"] in requested}
            if len(set(union)) != len(union) or set(union) != expected:
                raise EntryError("invalid_sorting_permutation", 409)
            for group_id, ids in requested.items():
                kind = all_groups[group_id]["kind"]
                for item_id in ids:
                    original = all_items[item_id]["group_id"]
                    if original != group_id and (kind not in ("colors", "memories")
                                                or all_groups[original]["kind"] != kind):
                        raise EntryError("invalid_sorting_group_move")
            changes = []
            for group_id, ids in requested.items():
                before = sorted((i for i, item in all_items.items() if item["group_id"] == group_id),
                                key=lambda i: (all_items[i]["position"], i))
                changes.append({"group_id": group_id, "before": before, "after": ids})
                for position, item_id in enumerate(ids):
                    db.execute("UPDATE price_sorting_items SET group_id=?,position=?,is_new=0 WHERE item_id=?",
                               (group_id, position, item_id))
            # Reassign the same occupied global slots, retaining every unrelated rank.
            # Aliases are never changed by sorting, and repeated canonical colours
            # still use their first occurrence, as in the legacy generator.
            for group_id, ids in requested.items():
                kind = all_groups[group_id]["kind"]
                before = next(change["before"] for change in changes if change["group_id"] == group_id)
                if before == ids:
                    continue
                pairs = [("model_order", "name")] if kind == "models" else \
                        [("memory_order", "name")] if kind == "memories" else \
                        [("raw_color_order", "name"), ("color_order", "canonical")] if kind == "colors" else \
                        [("supplier_category_order" if group_id == "categories:suppliers" else "category_order", "name")]
                for dimension, field in pairs:
                    names = list(dict.fromkeys(all_items[item_id][field] for item_id in ids
                                              if all_items[item_id][field]))
                    slots = sorted(_append_rank(db, dimension, name) for name in names)
                    db.executemany("UPDATE price_sorting_orders SET rank=? WHERE dimension=? AND name=?",
                                   [(rank, dimension, name) for rank, name in zip(slots, names)])
            db.execute("""UPDATE price_sorting_groups SET position=coalesce((
                SELECT rank FROM price_sorting_orders WHERE dimension='category_order'
                AND name=price_sorting_groups.title),position) WHERE kind='models' AND category_id IS NOT NULL""")
            _history(db, "reorder", f"Сохранён порядок: {len(requested)} разделов", changes, operation_id)
            result = {"status": "applied", "operation_id": operation_id, **_state(db)}
            db.execute("INSERT INTO price_sorting_operations VALUES (?,?,?,?)",
                       (operation_id, request_hash, now(), _json(result)))
            return result


def sort_rows_if_ready(db: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Read-only ordering for an already-open price-entry read transaction."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='price_sorting_state'").fetchone():
        return rows
    if not db.execute("SELECT 1 FROM price_sorting_state WHERE id=1").fetchone():
        return rows
    orders = {}
    for dimension, name, rank in db.execute("SELECT dimension,name,rank FROM price_sorting_orders"):
        orders.setdefault(dimension, {})[name] = rank
    aliases = dict(db.execute("SELECT raw_name,canonical FROM price_sorting_aliases"))
    def key(row):
        color = str(row.get("color") or "").strip()
        canonical = aliases.get(color.lower(), color)
        return (
            orders.get("category_order", {}).get(str(row.get("category_name") or "").strip(), 999999),
            orders.get("model_order", {}).get(str(row.get("model_name") or "").strip(), 999999),
            orders.get("memory_order", {}).get(str(row.get("memory") or "").strip(), 999999),
            orders.get("color_order", {}).get(canonical, 999999),
        )
    return sorted(rows, key=key)
