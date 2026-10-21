"""Site import formats must preserve exactly the configurations shown in preview."""
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from price_server.entry_catalog import EntryCatalogService, install_catalog
from price_server.entry_store import initialize
from price_server.model_import import prepare_model_import
from price_server.model_inbox import parse_model_drafts
from price_server.price_entry import EntryError
from test_price_entry import sample


RAY_BAN_COLORS = [
    "Havana/Green", "Matte Black/Clear",
    "Matte Black/Clear to Graphite Green Transitions", "Matte Black/Grey",
    "Shiny Black/Clear to Grey Transitions", "Shiny Black/Polarized Green",
    "Transparent Black/Clear to Aquamarine Transitions",
    "Transparent Matte Ocean Blue/Clear to Grey Transitions",
]
RAY_BAN_DRAFT = (
    "0. 120\n1. Ray Ban Meta Wayfarer Gen 3\n2. S(50mm), L(53mm)\n3. "
    + ", ".join(RAY_BAN_COLORS)
)


class ModelImportFormatTests(unittest.TestCase):
    def test_arbitrary_models_share_the_same_list_rules(self):
        cases = [
            ("Future Phone Pro (SIM + eSIM)", "8/256 GB, 12/512 GB", "Purple/Gold, Silver"),
            ("New Watch Titanium (Loop)", "42mm, 49mm", "Burgundy/Olive, Natural"),
            ("Other Glasses Gen 9", "S(50mm), L(53mm)", "Matte Ocean Blue/Clear, Havana/Green"),
            ("Unknown accessory №42", "", ""),
        ]
        for model, memories, colors in cases:
            with self.subTest(model=model):
                preview = prepare_model_import(f"0. 120\n1. {model}\n2. {memories}\n3. {colors}", 10)
                self.assertEqual(preview["models"][0]["model_name"], model)
                self.assertEqual(preview["sorting"]["memories"], memories.split(", ") if memories else [])
                self.assertEqual(preview["sorting"]["colors"], colors.split(", ") if colors else [])

    def test_alternate_separators_are_consistent_across_formats(self):
        for separator in (",", ";", "，", "；", "،"):
            for text in (
                f"1. New Model\n2. S(50mm){separator}L(53mm)\n3. Black/Clear{separator}Havana/Green",
                f"New Model | S(50mm){separator}L(53mm) | Black/Clear{separator}Havana/Green",
                f"New Model\nS(50mm){separator}L(53mm)\nBlack/Clear{separator}Havana/Green",
                f"Категория: Glasses\nМодель: New Model\nS(50mm) | Black/Clear{separator}Havana/Green",
            ):
                with self.subTest(separator=separator, text=text):
                    preview = prepare_model_import(text, 10)
                    self.assertEqual(preview["sorting"]["colors"], ["Black/Clear", "Havana/Green"])
                    self.assertEqual(preview["sorting"]["memories"][0], "S(50mm)")

    def test_numbered_continuations_match_the_original_preview(self):
        expected = prepare_model_import("0. 120\n1. Model\n2. S(50mm), L(53mm)\n3. Black/Clear, Havana/Green", 10)
        texts = [
            "0: 120\n1: Model\n2: S(50mm),\nL(53mm)\n3: Black/Clear;\nHavana/Green",
            "0）120\n1）Model\n2）\nS(50mm)\nL(53mm)\n3）\nBlack/Clear\nHavana/Green",
            "0. 120\n1. Model\n2. S(50mm)\n• L(53mm)\n3. Black/Clear\n• Havana/Green",
        ]
        for text in texts:
            for newline in ("\n", "\r\n", "\r"):
                with self.subTest(text=text, newline=repr(newline)):
                    preview = prepare_model_import("\ufeff" + text.replace("\n", newline), 10)
                    self.assertEqual(preview, expected)

    def test_multiple_numbered_blocks_preserve_different_categories_and_dimensions(self):
        preview = prepare_model_import(
            "0. 26\n1. Watch\n2. 42mm,\n46mm\n3. Black\n• Silver\n\n"
            "0: 120\n1: Glasses\n2: L(53mm)\n3: Clear/Green\n\n"
            "1. Cable\n2. 2m\n3. White", 10)
        self.assertEqual(preview["source_category_ids"], [26, 120])
        self.assertEqual([m["model_name"] for m in preview["models"]], ["Watch", "Glasses", "Cable"])
        self.assertEqual(preview["models"][1]["variants"], [{"memory": "L(53mm)", "color": "Clear/Green"}])
        self.assertEqual(preview["models"][2]["variants"], [{"memory": "2m", "color": "White"}])

    def test_numbered_errors_identify_the_field_and_physical_line(self):
        for prefix, field, size in ((1, "Модель", 251), (2, "Память / размер", 101), (3, "Цвета", 151)):
            lines = ["0. 120", "", "1. Model", "2. Size", "3. Color"]
            line_number = {1: 3, 2: 4, 3: 5}[prefix]
            lines[line_number - 1] = f"{prefix}. " + "x" * size
            with self.subTest(field=field), self.assertRaises(EntryError) as caught:
                prepare_model_import("\n".join(lines), 10)
            self.assertEqual(caught.exception.details["field"], field)
            self.assertEqual(caught.exception.details["line"], line_number)
            self.assertIn(field, caught.exception.details["message"])
        with self.assertRaises(EntryError) as caught:
            prepare_model_import("1. Model\n2. Size\n3. Color\nUnexpected unmarked text", 10)
        self.assertEqual(caught.exception.details["line"], 4)
        self.assertIn("маркер", caught.exception.details["message"])

    def test_empty_bom_text_is_a_validation_error(self):
        with self.assertRaises(EntryError):
            prepare_model_import("\ufeff  \r\n ", 10)

    def test_numbered_ray_ban_long_color_list_keeps_every_color_and_size(self):
        self.assertGreater(len(", ".join(RAY_BAN_COLORS)), 250)
        expected = [{"memory": memory, "color": color}
                    for memory in ("S(50mm)", "L(53mm)")
                    for color in RAY_BAN_COLORS + [""]]
        parsed = parse_model_drafts(RAY_BAN_DRAFT)
        self.assertEqual(parsed[0]["category_id"], 120)
        self.assertEqual(parsed[0]["model_name"], "Ray Ban Meta Wayfarer Gen 3")
        self.assertEqual(parsed[0]["variants"], expected)
        preview = prepare_model_import(RAY_BAN_DRAFT, 10)
        self.assertEqual(preview["variant_count"], 18)
        self.assertEqual(preview["models"][0]["variants"], expected)
        self.assertEqual(preview["models"][0]["category_id"], 10)
        self.assertEqual(preview["sorting"]["colors"], RAY_BAN_COLORS)
        self.assertEqual(preview["warnings"], ["source_category_ignored"])
        self.assertEqual(preview, prepare_model_import(RAY_BAN_DRAFT, 10))

    def test_numbered_long_memory_list_is_limited_per_item(self):
        memories = [f"Size {index:02d}(50mm)" for index in range(30)]
        self.assertGreater(len(", ".join(memories)), 250)
        text = "0. 120\n1. Watch\n2. " + ", ".join(memories) + "\n3. Black"
        preview = prepare_model_import(text, 10)
        self.assertEqual(preview["sorting"]["memories"], memories)
        self.assertEqual(preview["variant_count"], 30)

    def test_numbered_individual_field_and_overall_text_limits_remain(self):
        for text in (
            "1. " + "x" * 251 + "\n2. 50mm\n3. Black",
            "1. Watch\n2. " + "x" * 101 + "\n3. Black",
            "1. Watch\n2. 50mm\n3. " + "x" * 151,
            RAY_BAN_DRAFT + " " * 12000,
        ):
            with self.subTest(text=text[:30]), self.assertRaises(EntryError) as caught:
                prepare_model_import(text, 10)
            self.assertEqual(caught.exception.code, "invalid_model_draft")

    def test_numbered_long_lists_still_respect_variant_limit(self):
        colors = ", ".join(f"Named color {index:02d}" for index in range(19))
        self.assertGreater(len(colors), 250)
        def text(count):
            return "1. Watch\n2. " + ",".join(map(str, range(count))) + "\n3. " + colors
        self.assertEqual(prepare_model_import(text(25), 10)["variant_count"], 500)
        with self.assertRaises(EntryError) as caught:
            prepare_model_import(text(26), 10)
        self.assertEqual(caught.exception.code, "invalid_catalog_variants")

    def test_plain_blocks_expand_only_explicit_combinations(self):
        preview = prepare_model_import(
            "Phone Pro\n256 GB, 512 GB\nBlack, Silver\n\n"
            "Headphones\n-\nBlue\n\nCable\n-\n-", 10,
        )
        self.assertEqual(preview["model_count"], 3)
        self.assertEqual(preview["variant_count"], 6)
        self.assertEqual(preview["models"][0]["variants"], [
            {"memory": "256 GB", "color": "Black"},
            {"memory": "256 GB", "color": "Silver"},
            {"memory": "512 GB", "color": "Black"},
            {"memory": "512 GB", "color": "Silver"},
        ])
        self.assertEqual(preview["models"][1]["variants"], [{"memory": "", "color": "Blue"}])
        self.assertEqual(preview["models"][2]["variants"], [{"memory": "", "color": ""}])
        self.assertEqual(preview["source_category_ids"], [])
        self.assertEqual(preview["warnings"], [])

    def test_equivalent_spreadsheet_pipe_and_blocks_have_same_review_hash(self):
        texts = [
            "Phone Pro\n256 GB, 512 GB\nBlack, Silver\n\nCable\n-\n-",
            "Модель\tПамять\tЦвета\r\nPhone Pro\t256 GB, 512 GB\tBlack, Silver\r\nCable\t\t",
            "model | memory | colors\nPhone Pro | 256 GB, 512 GB | Black, Silver\nCable | - | -",
        ]
        previews = [prepare_model_import(text, 10) for text in texts]
        self.assertTrue(all(item == previews[0] for item in previews))
        self.assertEqual(previews[0]["variant_count"], 5)

    def test_tables_allow_no_header_and_singular_color_header(self):
        for text in (
            "Accessory\t\tBlack, White",
            "Модель | Память | Цвет\nAccessory | - | Black, White",
            "Model\tMemory\tColor\nAccessory\t-\tBlack, White",
        ):
            with self.subTest(text=text):
                preview = prepare_model_import(text, 10)
                self.assertEqual(preview["models"][0]["model_name"], "Accessory")
                self.assertEqual(preview["models"][0]["variants"], [
                    {"memory": "", "color": "Black"}, {"memory": "", "color": "White"},
                ])

    def test_explicit_blank_color_is_not_implicit(self):
        preview = prepare_model_import("Phone | 256 GB | Black, -", 10)
        self.assertEqual(preview["models"][0]["variants"], [
            {"memory": "256 GB", "color": "Black"}, {"memory": "256 GB", "color": ""},
        ])

    def test_legacy_numbered_and_labelled_formats_keep_their_behavior(self):
        numbered = prepare_model_import("0. 26\n1. Watch\n2. 42mm, 46mm\n3. Black, Silver", 10)
        self.assertEqual(numbered["variant_count"], 6)
        self.assertIn({"memory": "42mm", "color": ""}, numbered["models"][0]["variants"])
        self.assertEqual(numbered["warnings"], ["source_category_ignored"])
        self.assertEqual(numbered["source_category_ids"], [26])
        labelled = prepare_model_import(
            "Добавить модель\nКатегория: Watches\nМодель: Watch\n42mm | Black, Silver\n46mm | Black", 10,
        )
        self.assertEqual(labelled["variant_count"], 3)
        legacy_plain = prepare_model_import("Watches\nWatch\n42mm | Black, Silver\n46mm | Black", 10)
        self.assertEqual(legacy_plain, labelled)

    def test_repeated_model_name_reports_its_line_and_block(self):
        with self.assertRaises(EntryError) as caught:
            prepare_model_import("Phone\n128 GB\nBlack\n\n phone \n256 GB\nWhite", 10)
        self.assertEqual(caught.exception.code, "duplicate_catalog_model")
        self.assertEqual(caught.exception.details["line"], 5)
        self.assertEqual(caught.exception.details["block"], 2)
        with self.assertRaises(EntryError) as caught:
            prepare_model_import("Model\tMemory\tColor\nPhone\t128\tBlack\nPHONE\t256\tBlue", 10)
        self.assertEqual(caught.exception.code, "duplicate_catalog_model")
        self.assertEqual(caught.exception.details["line"], 3)

    def test_repeated_configuration_values_are_not_silently_multiplied(self):
        for text in ("Phone | 128, 128 | Black", "Phone\n128\nBlack, black"):
            with self.subTest(text=text), self.assertRaises(EntryError) as caught:
                prepare_model_import(text, 10)
            self.assertEqual(caught.exception.code, "duplicate_catalog_variant")
            self.assertEqual(caught.exception.details["line"], 1)

    def test_bad_table_rows_report_physical_line(self):
        for text in (
            "Model\tMemory\tColors\n\nPhone\t128",
            "Model | Memory | Colors\n\nPhone | 128 | Black | unexpected",
        ):
            with self.subTest(text=text), self.assertRaises(EntryError) as caught:
                prepare_model_import(text, 10)
            self.assertEqual(caught.exception.code, "model_import_columns_required")
            self.assertEqual(caught.exception.details["line"], 3)

    def test_missing_block_dimension_explains_dash_placeholder(self):
        with self.assertRaises(EntryError) as caught:
            prepare_model_import("Phone\n128\nBlack\n\nAccessory\nBlue", 10)
        self.assertEqual(caught.exception.code, "model_import_block_format")
        self.assertEqual(caught.exception.details["line"], 5)
        self.assertEqual(caught.exception.details["block"], 2)
        self.assertIn("«-»", caught.exception.details["message"])

    def test_empty_name_or_header_without_rows_is_rejected(self):
        for text in ("\t128\tBlack", "- | 128 | Black", "Модель\tПамять\tЦвета"):
            with self.subTest(text=text), self.assertRaises(EntryError):
                prepare_model_import(text, 10)

    def test_model_variant_and_overall_limits_are_enforced(self):
        cases = [
            ("\n".join(f"Phone {index}\t-\t-" for index in range(101)), 100),
            ("Phone\n" + ",".join(map(str, range(26))) + "\n" + ",".join(map(str, range(20))), 500),
            ("\n".join(f"Phone {index}\t" + ",".join(map(str, range(25))) + "\t" +
                       ",".join(map(str, range(20))) for index in range(11)), 5000),
        ]
        for text, limit in cases:
            with self.subTest(limit=limit), self.assertRaises(EntryError) as caught:
                prepare_model_import(text, 10)
            self.assertEqual(caught.exception.code, "invalid_catalog_variants")
            self.assertEqual(caught.exception.details["limit"], limit)

    def test_invalid_values_and_text_limit_are_enforced(self):
        for text in ("x" * 251 + " | - | -", "Phone | " + "x" * 101 + " | Black",
                     "Phone | - | " + "x" * 151, "Phone | - | Bl\x00ack"):
            with self.subTest(text=text[:20]), self.assertRaises(EntryError) as caught:
                prepare_model_import(text, 10)
            self.assertEqual(caught.exception.code, "invalid_model_import_value")
            self.assertEqual(caught.exception.details["line"], 1)
        with self.assertRaises(EntryError) as caught:
            prepare_model_import("x" * 12001, 10)
        self.assertEqual(caught.exception.code, "invalid_model_draft")
        self.assertIn("12000", caught.exception.details["message"])


class ModelImportFormatApplyTests(unittest.TestCase):
    def test_numbered_long_list_preview_and_apply_preserve_all_variants(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "price.db"
            initialize(path, [{"sheet_id": 1, "title": "1-Supplier", "rows": [sample()]}])
            install_catalog(path, [{"category_id": 10, "name": "Phones"}],
                            max_product_id=100, max_version_id=200)
            service = EntryCatalogService(path)
            preview = service.preview_import(RAY_BAN_DRAFT, 10)
            operation = str(uuid.uuid4())
            result = service.import_models(RAY_BAN_DRAFT, 10, preview["preview_hash"], operation)
            self.assertEqual(result["created_count"], 18)
            self.assertEqual(service.import_models(RAY_BAN_DRAFT, 10, preview["preview_hash"], operation), result)
            actual = service.model(101)
            self.assertEqual(actual["model_name"], "Ray Ban Meta Wayfarer Gen 3")
            self.assertEqual([{k: item[k] for k in ("memory", "color")}
                              for item in actual["variants"]], preview["models"][0]["variants"])
            self.assertEqual(len(actual["variants"]), 18)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_new_formats_preview_and_apply_exact_variants_idempotently(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "price.db"
            initialize(path, [{"sheet_id": 1, "title": "1-Supplier", "rows": [sample()]}])
            install_catalog(path, [{"category_id": 10, "name": "Phones"}],
                            max_product_id=100, max_version_id=200)
            service = EntryCatalogService(path)
            text = "Phone Pro | 128, 256 | Black, Silver\nAccessory | - | -"
            preview = service.preview_import(text, 10)
            self.assertEqual(preview["variant_count"], 5)
            operation = str(uuid.uuid4())
            result = service.import_models(text, 10, preview["preview_hash"], operation)
            self.assertEqual(result["created_count"], 5)
            self.assertEqual(result["model_count"], 2)
            self.assertEqual(service.import_models(text, 10, preview["preview_hash"], operation), result)
            self.assertEqual(service.model(101)["variants"][0]["memory"], "128")
            self.assertTrue(all(row["color"] for row in service.model(101)["variants"]))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM entry_products").fetchone()[0], 6)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(EntryError) as caught:
                service.preview_import(text, 10)
            self.assertEqual(caught.exception.code, "catalog_model_exists")


if __name__ == "__main__":
    unittest.main()
