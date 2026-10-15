import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from price_server.price_entry import EntryError
from price_server.sorting import SortingService, install_sorting, sort_rows_if_ready


class SortingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "price.db"
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE entry_categories(category_id INTEGER PRIMARY KEY,name TEXT)")
            db.executemany("INSERT INTO entry_categories VALUES (?,?)", [(1, "Phones"), (2, "Watches")])
            db.execute("CREATE TABLE entry_products(product_key TEXT PRIMARY KEY,position INTEGER,metadata_json TEXT)")
        self.product(1, "Alpha", "128 GB", "Black")
        self.product(1, "Beta", "256 GB", "White")
        self.product(2, "Watch", "42mm", "Black")
        self.seed = {
            "models": ["▸ Phones", "Alpha", "Legacy", "Beta", "Watch"],
            "category_names": ["Phones", "Watches"],
            "supplier_category_names": ["Watches", "Phones"],
            "memories": ["128 GB", "42mm", "256 GB"],
            "colors": [
                {"name": "Black", "canonical": "Black", "group": "Чёрные"},
                {"name": "Black Leather", "canonical": "Black", "group": "Чёрные"},
                {"name": "Mystery", "canonical": None, "group": "Другие цвета"},
                {"name": "White", "canonical": "White", "group": "Белые"},
            ],
            "orders": {
                "model_order": {"▸ Phones": 0, "Alpha": 1, "Legacy": 2, "Beta": 3, "Watch": 4},
                "category_order": {"Phones": 0, "Watches": 1},
                "memory_order": {"128 GB": 0, "42mm": 1, "256 GB": 2},
                "color_order": {"Black": 0, "White": 1},
                "color_replace_dict": {"black": "Black", "black leather": "Black", "white": "White"},
            },
            "catalog_groups": [{"group_id": "phones", "title": "Телефоны", "sections": []}],
        }
        self.service = SortingService(self.path)

    def product(self, category_id, name, memory="", color=""):
        with sqlite3.connect(self.path) as db:
            position = db.execute("SELECT coalesce(max(position),-1)+1 FROM entry_products").fetchone()[0]
            category = db.execute("SELECT name FROM entry_categories WHERE category_id=?", (category_id,)).fetchone()[0]
            db.execute("INSERT INTO entry_products VALUES (?,?,?)", (str(position + 1), position, json.dumps({
                "product_id": position + 1, "category_id": category_id, "category_name": category,
                "model_name": name, "memory": memory, "color": color, "price_1": 125,
            })))

    def install(self):
        return install_sorting(self.path, self.seed)

    def members(self, state, group_id):
        return [item for item in state["items"] if item["group_id"] == group_id]

    def request(self, state, group_id="models:1", reverse=True):
        ids = [item["id"] for item in self.members(state, group_id)]
        return {"revision": state["revision"], "groups": [{"group_id": group_id,
                "item_ids": list(reversed(ids)) if reverse else ids}]}

    def save(self, body, operation=None):
        return self.service.save(body, operation or str(uuid.uuid4()))

    def test_missing_seed_fails_closed_without_creating_empty_sorting(self):
        with self.assertRaises(EntryError) as caught:
            self.service.state()
        self.assertEqual(caught.exception.code, "sorting_not_initialized")
        self.assertEqual(caught.exception.status, 503)
        with sqlite3.connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='price_sorting_state'").fetchone())

    def test_initial_exact_order_aliases_and_catalog_are_preserved(self):
        with sqlite3.connect(self.path) as db:
            products = db.execute("SELECT * FROM entry_products").fetchall()
        state = self.install()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(sum(group["new_count"] for group in state["groups"]), 0)
        export = self.service.export()
        for dimension, values in self.seed["orders"].items():
            self.assertEqual(export[dimension], values)
        self.assertEqual(export["catalog_groups"], self.seed["catalog_groups"])
        self.assertNotIn("mystery", export["color_replace_dict"])
        content_hash = export.pop("content_hash")
        self.assertEqual(content_hash, hashlib.sha256(json.dumps(export, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()).hexdigest())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM entry_products").fetchall(), products)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_install_is_idempotent_and_cannot_overwrite_user_reorder(self):
        state = self.install()
        changed = self.save(self.request(state))
        again = self.install()
        self.assertEqual(again["status"], "already_initialized")
        self.assertEqual(again["revision"], changed["revision"])
        self.assertEqual(self.members(again, "models:1")[0]["name"], "Beta")
        self.seed["models"].append("Unexpected")
        with self.assertRaises(EntryError) as caught:
            self.install()
        self.assertEqual(caught.exception.code, "sorting_migration_conflict")

    def test_reorder_only_reassigns_occupied_slots(self):
        state = self.install()
        result = self.save(self.request(state))
        order = self.service.export()["model_order"]
        self.assertEqual(order, {"▸ Phones": 0, "Beta": 1, "Legacy": 2, "Alpha": 3, "Watch": 4})
        self.assertEqual(result["revision"], state["revision"] + 1)
        self.assertEqual(result["history"][0]["action"], "reorder")
        self.assertEqual(SortingService(self.path).state()["items"], result["items"])

    def test_auto_discovery_appends_to_category_and_marks_new(self):
        original = self.install()
        self.product(1, "A Newly Added", "1 TB", "Dark Black")
        state = self.service.state()
        models = self.members(state, "models:1")
        self.assertEqual([item["name"] for item in models], ["Alpha", "Beta", "A Newly Added"])
        self.assertTrue(models[-1]["is_new"])
        color = next(item for item in state["items"] if item["name"] == "Dark Black")
        black = next(item for item in state["items"] if item["name"] == "Black")
        self.assertEqual(color["group_id"], black["group_id"])
        self.assertEqual(self.members(state, black["group_id"])[-1]["name"], "Dark Black")
        self.assertEqual(self.service.state()["revision"], state["revision"])
        self.assertGreater(state["revision"], original["revision"])
        export = self.service.export()
        self.assertEqual(export["color_replace_dict"]["dark black"], "Dark Black")
        self.assertIn("1 TB", export["memory_order"])
        reviewed = self.save(self.request(state, reverse=False))
        self.assertTrue(all(not item["is_new"] for item in self.members(reviewed, "models:1")))

    def test_discovery_new_category_and_rename_preserve_identity(self):
        self.install()
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO entry_categories VALUES (3,'Audio')")
        state = self.service.state()
        self.assertIn("models:3", [group["id"] for group in state["groups"]])
        self.assertEqual(self.service.export()["category_order"]["Audio"], 2)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE entry_categories SET name='Smartphones' WHERE category_id=1")
        state = self.service.state()
        self.assertEqual(next(group for group in state["groups"] if group["id"] == "models:1")["title"], "Smartphones")
        self.assertEqual(next(item for item in state["items"] if item["id"] == "category:price:1")["name"], "Smartphones")
        order = self.service.export()["category_order"]
        self.assertNotIn("Phones", order)
        self.assertEqual(order["Smartphones"], 0)

    def test_deleted_model_becomes_inactive_but_not_lost(self):
        self.install()
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM entry_products WHERE product_key='1'")
        state = self.service.state()
        item = next(item for item in state["items"] if item["name"] == "Alpha")
        self.assertFalse(item["active"])
        self.product(1, "Alpha")
        state = self.service.state()
        self.assertTrue(next(i for i in state["items"] if i["id"] == item["id"])["active"])

    def test_idempotency_replay_precedes_revision_and_discovery(self):
        state = self.install()
        body, operation = self.request(state), str(uuid.uuid4())
        result = self.save(body, operation)
        self.product(1, "New")
        self.assertEqual(self.save(body, operation), result)
        body["groups"][0]["item_ids"].reverse()
        with self.assertRaises(EntryError) as caught:
            self.save(body, operation)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_stale_revision_fails_and_writes_nothing(self):
        state = self.install()
        body = self.request(state)
        self.save(body)
        before = self.service.state()
        with self.assertRaises(EntryError) as caught:
            self.save(body)
        self.assertEqual(caught.exception.code, "sorting_revision_conflict")
        self.assertEqual(self.service.state(), before)

    def test_invalid_permutations_are_atomic(self):
        state = self.install()
        for ids in ([], ["missing"], [self.members(state, "models:1")[0]["id"]] * 2):
            body = self.request(state)
            body["groups"][0]["item_ids"] = ids
            with self.assertRaises(EntryError):
                self.save(body)
            self.assertEqual(self.service.state(), {key: value for key, value in state.items() if key != "status"})

    def test_model_cannot_move_to_another_category(self):
        state = self.install()
        first = [item["id"] for item in self.members(state, "models:1")]
        second = [item["id"] for item in self.members(state, "models:2")]
        with self.assertRaises(EntryError) as caught:
            self.save({"revision": state["revision"], "groups": [
                {"group_id": "models:1", "item_ids": second},
                {"group_id": "models:2", "item_ids": first}]})
        self.assertEqual(caught.exception.code, "invalid_sorting_group_move")

    def test_color_move_between_groups_is_permutation_safe(self):
        state = self.install()
        black = next(i for i in state["items"] if i["name"] == "Black")
        white = next(i for i in state["items"] if i["name"] == "White")
        first = [i["id"] for i in self.members(state, black["group_id"])]
        second = [i["id"] for i in self.members(state, white["group_id"])]
        moved = first.pop()
        second.append(moved)
        result = self.save({"revision": state["revision"], "groups": [
            {"group_id": black["group_id"], "item_ids": first},
            {"group_id": white["group_id"], "item_ids": second}]})
        self.assertEqual(next(i for i in result["items"] if i["id"] == moved)["group_id"], white["group_id"])
        self.assertEqual(self.service.export()["color_replace_dict"], self.seed["orders"]["color_replace_dict"])

    def test_competing_saves_only_one_wins(self):
        state = self.install()
        body = self.request(state)
        def work(_):
            try:
                return self.save(body)["status"]
            except EntryError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(work, range(2)))
        self.assertEqual(sorted(outcomes), ["applied", "sorting_revision_conflict"])

    def test_read_only_row_sort_preserves_payload_and_original_row_numbers(self):
        self.install()
        rows = [{"model_name": "Beta", "category_name": "Phones", "memory": "256 GB", "color": "White", "row": 2},
                {"model_name": "Alpha", "category_name": "Phones", "memory": "128 GB", "color": "Black", "row": 3}]
        original = copy.deepcopy(rows)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA query_only=ON")
            sorted_rows = sort_rows_if_ready(db, rows)
        self.assertEqual([item["row"] for item in sorted_rows], [3, 2])
        self.assertEqual(rows, original)

    def test_unchanged_review_does_not_change_any_exported_order(self):
        state = self.install()
        before = self.service.export()
        body = {"revision": state["revision"], "groups": [
            {"group_id": group["id"], "item_ids": [i["id"] for i in self.members(state, group["id"])]}
            for group in state["groups"]]}
        self.save(body)
        after = self.service.export()
        for key in ("model_order", "category_order", "supplier_category_order", "color_order",
                    "color_replace_dict", "raw_color_order", "memory_order", "catalog_groups"):
            self.assertEqual(after[key], before[key])

    def test_category_reorder_immediately_reorders_model_groups(self):
        state = self.install()
        changed = self.save(self.request(state, "categories:price"))
        groups = [group["id"] for group in changed["groups"] if group["id"] in ("models:1", "models:2")]
        self.assertEqual(groups, ["models:2", "models:1"])

    def test_reorder_multiple_groups_keeps_each_groups_own_global_slots(self):
        self.seed["models"] += ["Second Watch"]
        self.seed["orders"]["model_order"] = {"Alpha": 0, "Watch": 1, "Beta": 2, "Second Watch": 3}
        self.product(2, "Second Watch")
        state = self.install()
        self.save({"revision": state["revision"], "groups": [
            self.request(state, group_id)["groups"][0] for group_id in ("models:2", "models:1")]})
        self.assertEqual(self.service.export()["model_order"],
                         {"Beta": 0, "Second Watch": 1, "Alpha": 2, "Watch": 3})

    def test_invalid_seed_does_not_leave_partial_installation(self):
        self.seed["orders"]["model_order"] = {"Alpha": 0, "Beta": 0}
        with self.assertRaises(EntryError):
            self.install()
        with sqlite3.connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='price_sorting_state'").fetchone())

    def test_operation_uuid_is_required(self):
        state = self.install()
        with self.assertRaises(EntryError) as caught:
            self.service.save(self.request(state), "")
        self.assertEqual(caught.exception.code, "idempotency_key_required")


if __name__ == "__main__":
    unittest.main()
