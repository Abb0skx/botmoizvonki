import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
import pandas as pd

from price_server import worker_sorting
from price_server.worker_sorting import (
    decode, load_server_sorting, load_server_sorting_payload, load_server_sorting_rows, sort_price_rows,
)
from scripts.prepare_price_worker_sorting import (
    EXCEL_OPEN_ANCHOR, EXCEL_ROWS_ANCHOR, PRICE_ANCHOR, patch_excel, patch_price,
    patch_format_prices, prepare,
)

FORMAT_FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "price_worker_formatting.py"


def sign(data):
    data = copy.deepcopy(data)
    data.pop("content_hash", None)
    data["content_hash"] = hashlib.sha256(json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    return data


def sample():
    return sign({
        "schema_version": 1, "source": "sqlite", "revision": 3,
        "updated_at": "2026-10-15T08:00:00+05:00",
        "color_replace_dict": {"pearl white": "White"},
        "category_order": {"Телефоны": 0, "Часы": 2},
        "model_order": {"iPhone": 0, "Watch": 4},
        "memory_order": {"128 GB": 0, "256 GB": 1},
        "color_order": {"Black": 0, "White": 1},
        "supplier_category_order": {"Телефоны": 0},
        "raw_color_order": {"Black": 0, "Pearl White": 1},
        "catalog_groups": [{
            "position": 0, "group_id": "phones", "title": "Телефоны", "sections": [{
                "position": 0, "section_key": "phones-apple", "catalog_label": "Apple",
                "title": "Телефоны Apple", "category_names": ["Телефоны"],
                "include_model_pattern": "iPhone", "exclude_model_pattern": None,
                "notice_text": "Цены могут меняться.",
                "notice_html": '<p class="price-notice">Цены могут меняться.</p>',
                "enabled": True,
            }],
        }],
    })


class Response:
    def __init__(self, payload=None, *, status=200, raw=None, error=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps(payload).encode()
        self.error = error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def iter_content(self, size):
        if self.error:
            raise self.error
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start + size]


class WorkerSortingTests(unittest.TestCase):
    def test_round_trip_preserves_ranks_replacements_and_catalog(self):
        payload = sample()
        expected = copy.deepcopy(payload)
        expected.pop("content_hash")
        self.assertEqual(decode(payload), expected)
        self.assertIn("content_hash", payload)

    def test_checksum_and_schema_are_strict(self):
        for field, value in (("content_hash", "a" * 64), ("content_hash", True),
                             ("schema_version", True), ("schema_version", 2),
                             ("source", "google_sheets")):
            with self.subTest(field=field, value=value):
                payload = sample()
                payload[field] = value
                if field != "content_hash":
                    payload = sign(payload)
                with self.assertRaisesRegex(RuntimeError, "invalid schema/checksum"):
                    decode(payload)
        for payload in ([], None, 1, "text", {"not": "a sorting export"}):
            with self.assertRaises(RuntimeError):
                decode(payload)

    def test_non_json_and_nonfinite_payloads_rejected(self):
        for value in (float("nan"), float("inf"), object(), "\ud800"):
            payload = sample()
            payload["unexpected"] = value
            with self.assertRaisesRegex(RuntimeError, "invalid schema/checksum"):
                decode(payload)

    def test_order_values_rejected_without_echoing_payload(self):
        for value in (-1, True, "0", 1.2, None, 1000001):
            payload = sample()
            payload["model_order"] = {"private injected content": value}
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "invalid ordering") as error:
                decode(sign(payload))
            self.assertNotIn("private injected content", str(error.exception))
        for field, value in (("category_order", {}), ("model_order", {}),
                             ("memory_order", []), ("raw_color_order", {"x": False}),
                             ("color_order", {"": 0})):
            payload = sample()
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                decode(sign(payload))

    def test_empty_memory_and_colors_supported_for_new_catalog(self):
        payload = sample()
        payload.update(memory_order={}, color_order={}, color_replace_dict={})
        self.assertEqual(decode(sign(payload))["memory_order"], {})

    def test_historical_multiline_model_names_preserved_exactly(self):
        payload = sample()
        name = "Asus Expertbook B1402 |Intel i5-1335U |\nIntel Iris Xe Graphics |DDR4 8Gb |SSD 512Gb |\n14 FullHD |Black"
        payload["model_order"][name] = 7
        self.assertEqual(decode(sign(payload))["model_order"][name], 7)

    def test_invalid_revisions_and_replacements(self):
        for field, value in (("revision", True), ("revision", -1), ("revision", ""),
                             ("updated_at", None), ("color_replace_dict", []),
                             ("color_replace_dict", {"White": "White"}),
                             ("color_replace_dict", {" white": "White"}),
                             ("color_replace_dict", {"white": None})):
            payload = sample()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                decode(sign(payload))

    def test_invalid_catalogs(self):
        for groups in (None, [], [None], [{}]):
            payload = sample()
            payload["catalog_groups"] = groups
            with self.subTest(groups=groups), self.assertRaises(RuntimeError):
                decode(sign(payload))
        for field, value in (("group_id", "invalid slug!"), ("position", True),
                             ("title", "Title\nnew line"), ("sections", [])):
            payload = sample()
            payload["catalog_groups"][0][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                decode(sign(payload))
        for field, value in (("section_key", "../bad"), ("position", -1),
                             ("category_names", []), ("category_names", ["Phones", "Phones"]),
                             ("enabled", 1), ("notice_html", None),
                             ("include_model_pattern", "[broken"), ("title", "")):
            payload = sample()
            payload["catalog_groups"][0]["sections"][0][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                decode(sign(payload))

    def test_duplicate_catalog_id_positions_and_html_ids_rejected(self):
        for field, value in (("section_key", "phones-apple"), ("position", 0),
                             ("section_key", "catalog-group-phones"),
                             ("section_key", "price-content"),
                             ("section_key", "phones-apple-content")):
            payload = sample()
            section = dict(payload["catalog_groups"][0]["sections"][0],
                           section_key="phones-other", position=1)
            section[field] = value
            payload["catalog_groups"][0]["sections"].append(section)
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                decode(sign(payload))

    def test_factory_recreates_existing_tuple_and_preserves_disabled_sections(self):
        data = decode(sample())
        entry = dict(data["catalog_groups"][0]["sections"][0],
                     position=2, section_key="phones-hidden", enabled=False)
        data["catalog_groups"][0]["sections"].insert(0, entry)
        with patch("price_server.worker_sorting.load_server_sorting_payload", return_value=data):
            output = load_server_sorting(SimpleNamespace, SimpleNamespace)
        self.assertEqual(output[:5], tuple(data[field] for field in (
            "color_replace_dict", "category_order", "model_order", "memory_order", "color_order")))
        self.assertEqual(output[5][0].label, "Телефоны")
        self.assertEqual(output[5][0].entries[0].section_id, "phones-apple")
        self.assertEqual(output[5][0].entries[0].category_names, ("Телефоны",))
        self.assertEqual(output[5][0].entries[1].enabled, False)
        self.assertEqual(output[5][0].entries[0].notice_html,
                         data["catalog_groups"][0]["sections"][1]["notice_html"])

    def test_supplier_rows_keep_raw_colors_and_supplier_categories(self):
        data = decode(sample())
        data["supplier_category_order"] = {"Watches": 9, "Phones": 3}
        data["raw_color_order"] = {"Pearl White": 10, "Black": 0}
        with patch("price_server.worker_sorting.load_server_sorting_payload", return_value=data):
            sheet, rows, headers = load_server_sorting_rows()
        self.assertEqual(sheet.title, "Website sorting")
        self.assertEqual(rows[0], headers)
        self.assertEqual(rows[1], ["Phones", "iPhone", "128 GB", "Black"])
        self.assertEqual(rows[2], ["Watches", "Watch", "256 GB", "Pearl White"])

    def test_supplier_export_rejects_missing_specific_orders(self):
        data = decode(sample())
        data.pop("raw_color_order")
        with patch("price_server.worker_sorting.load_server_sorting_payload", return_value=data), \
                self.assertRaisesRegex(RuntimeError, "supplier sorting is not initialized"):
            load_server_sorting_rows()

    @patch.dict(os.environ, {"PRICE_SERVER_URL": "https://price.example", "PRICE_SYNC_API_KEY": "k" * 32})
    def test_http_request_is_authenticated_bounded_and_does_not_redirect(self):
        response = Response(sample())
        with patch("price_server.worker_sorting.requests.get", return_value=response) as get:
            result = load_server_sorting_payload()
        self.assertEqual(result["revision"], 3)
        self.assertTrue(response.closed)
        get.assert_called_once_with(
            "https://price.example/price/api/v1/entry/sorting/export",
            headers={"X-Price-Sync-Key": "k" * 32}, timeout=(10, 60),
            allow_redirects=False, stream=True,
        )

    def test_invalid_configuration_never_requests_network_or_exposes_value(self):
        for url in ("", "http://example.com", "https://user:secret@example.com",
                    "https://example.com?secret=query", "https://example.com#secret",
                    "https://example.com:not-a-port", "https://[invalid"):
            with patch.dict(os.environ, {"PRICE_SERVER_URL": url, "PRICE_SYNC_API_KEY": "secret" * 6}), \
                    patch("price_server.worker_sorting.requests.get") as get, \
                    self.subTest(url=url), self.assertRaises(RuntimeError) as error:
                load_server_sorting_payload()
            get.assert_not_called()
            self.assertNotIn("secret", str(error.exception))
        with patch.dict(os.environ, {"PRICE_SERVER_URL": "https://example.com", "PRICE_SYNC_API_KEY": "short"}), \
                self.assertRaises(RuntimeError):
            load_server_sorting_payload()

    @patch.dict(os.environ, {"PRICE_SERVER_URL": "https://price.example", "PRICE_SYNC_API_KEY": "k" * 32})
    def test_http_errors_and_invalid_json_stop_without_fallback(self):
        for response, expected in ((Response(status=302), "HTTP 302"),
                                   (Response(status=401), "HTTP 401"),
                                   (Response(status=503), "HTTP 503"),
                                   (Response(raw=b"secret invalid json"), "invalid JSON"),
                                   (Response(raw=b"\xff"), "invalid JSON"),
                                   (Response(raw=b'{"same":1,"same":2}'), "duplicate JSON key")):
            with patch("price_server.worker_sorting.requests.get", return_value=response) as get, \
                    self.subTest(expected=expected), self.assertRaisesRegex(RuntimeError, expected) as error:
                load_server_sorting_payload()
            self.assertEqual(get.call_count, 1)
            self.assertTrue(response.closed)
            self.assertNotIn("secret", str(error.exception))

    @patch.dict(os.environ, {"PRICE_SERVER_URL": "https://price.example", "PRICE_SYNC_API_KEY": "k" * 32})
    def test_oversized_payload_and_network_errors_are_sanitized(self):
        with patch("price_server.worker_sorting.MAX_RESPONSE_BYTES", 4), \
                patch("price_server.worker_sorting.requests.get", return_value=Response(raw=b"12345")), \
                self.assertRaisesRegex(RuntimeError, "payload too large"):
            load_server_sorting_payload()
        with patch("price_server.worker_sorting.requests.get", side_effect=requests.Timeout("secret")), \
                self.assertRaisesRegex(RuntimeError, "fallback is disabled") as error:
            load_server_sorting_payload()
        self.assertNotIn("secret", str(error.exception))
        with patch("price_server.worker_sorting.requests.get", return_value=Response(error=requests.ConnectionError("secret"))), \
                self.assertRaisesRegex(RuntimeError, "fallback is disabled"):
            load_server_sorting_payload()


class WorkerPatchTests(unittest.TestCase):
    PRICE = (PRICE_ANCHOR + '        "url", "PRODUCT_SORT",\n    )\n    return content\n\n'
             + FORMAT_FIXTURE.read_text())
    EXCEL = EXCEL_OPEN_ANCHOR + '\nrequired_sort_columns = []\n' + EXCEL_ROWS_ANCHOR

    def test_patches_idempotent_and_do_not_hide_unexpected_source(self):
        for source, apply in ((self.PRICE, patch_price), (self.EXCEL, patch_excel)):
            patched = apply(source)
            self.assertEqual(apply(patched), patched)
            self.assertIn('"PRICE_SORT_SOURCE"', patched)
            with self.assertRaises(ValueError):
                apply("print('changed worker source')")
        with self.assertRaises(ValueError):
            patch_price(self.PRICE + self.PRICE)

    def test_prepare_checks_hashes_preserves_source_and_replays_safely(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "original", Path(temp) / "overlay"
            source.mkdir()
            (source / "Price.py").write_text(self.PRICE)
            (source / "Excel new.py").write_text(self.EXCEL)
            hashes = [hashlib.sha256(text.encode()).hexdigest() for text in (self.PRICE, self.EXCEL)]
            result = prepare(source, output, *hashes)
            self.assertEqual(prepare(source, output, *hashes), result)
            self.assertEqual((source / "Price.py").read_text(), self.PRICE)
            self.assertEqual((source / "Excel new.py").read_text(), self.EXCEL)
            self.assertEqual(set(result), {"Price.py", "Excel new.py", "server_sorting_source.py"})
            with self.assertRaisesRegex(ValueError, "checksum"):
                prepare(source, Path(temp) / "bad", "incorrect", hashes[1])
            self.assertFalse((Path(temp) / "bad").exists())
            with self.assertRaisesRegex(ValueError, "separate"):
                prepare(source, source, *hashes)
            (output / "Price.py").write_text("other contents")
            with self.assertRaisesRegex(ValueError, "different files"):
                prepare(source, output, *hashes)


class WorkerFormattingTests(unittest.TestCase):
    def renderers(self):
        source = FORMAT_FIXTURE.read_text()
        old, new = {}, {"os": os}
        exec(compile(source, str(FORMAT_FIXTURE), "exec"), old)
        exec(compile(patch_format_prices(source), "<website formatter>", "exec"), new)
        return old["format_prices"], new["format_prices"]

    def group(self, rows):
        return pd.DataFrame([dict(row, model_with_link="Example Model", status="") for row in rows])

    @patch.dict(os.environ, {"PRICE_SORT_SOURCE": "server"})
    @patch.dict(sys.modules, {"server_sorting_source": worker_sorting})
    def test_memory_order_honored_before_price_and_color_ties_stable(self):
        group = self.group([
            {"memory": "256 GB", "color": "White", "price": 200},
            {"memory": "256 GB", "color": "Black", "price": 200},
            {"memory": "256 GB", "color": "Red", "price": 150},
            {"memory": "128 GB", "color": "White", "price": 100},
            {"memory": "128 GB", "color": "Black", "price": 100},
        ])
        original = group.copy(deep=True)
        old, new = self.renderers()
        before, after = old(group), new(group)
        self.assertLess(before.index("128 GB"), before.index("256 GB"))
        self.assertLess(after.index("256 GB"), after.index("128 GB"))
        self.assertLess(after.index("Red"), after.index("White, Black"))
        self.assertIn("White, Black", after)
        self.assertEqual(sorted(re.findall(r"<strong>.*?</strong>", before)),
                         sorted(re.findall(r"<strong>.*?</strong>", after)))
        self.assertEqual(sorted(before.split("<br>")), sorted(after.split("<br>")))
        pd.testing.assert_frame_equal(group, original)
        self.assertEqual(set(sort_price_rows(group).columns), set(group.columns))

    @patch.dict(os.environ, {"PRICE_SORT_SOURCE": "server"})
    @patch.dict(sys.modules, {"server_sorting_source": worker_sorting})
    def test_color_order_changes_inside_equal_price_group_and_html_escaping_unchanged(self):
        old, new = self.renderers()
        group = self.group([
            {"memory": "", "color": "White & Gold", "price": 75},
            {"memory": "", "color": "<Black>", "price": 75},
            {"memory": "", "color": "Red", "price": 50},
        ])
        rendered = new(group)
        self.assertIn("White &amp; Gold, &lt;Black&gt;", rendered)
        self.assertLess(rendered.index("Red"), rendered.index("White"))
        self.assertEqual(rendered, old(group))
        reordered = new(group.iloc[[1, 0, 2]])
        self.assertIn("&lt;Black&gt;, White &amp; Gold", reordered)
        self.assertEqual(sorted(re.findall(r"<strong>.*?</strong>", rendered)),
                         sorted(re.findall(r"<strong>.*?</strong>", reordered)))

    @patch.dict(os.environ, {"PRICE_SORT_SOURCE": "server"})
    @patch.dict(sys.modules, {"server_sorting_source": worker_sorting})
    def test_memory_only_price_only_and_empty_cases(self):
        old, new = self.renderers()
        memory_group = self.group([
            {"memory": "512 GB", "price": 300},
            {"memory": "128 GB", "price": 100},
        ])
        rendered = new(memory_group)
        self.assertLess(rendered.index("512 GB"), rendered.index("128 GB"))
        self.assertEqual(sorted(old(memory_group).split("<br>")), sorted(rendered.split("<br>")))
        price_group = self.group([{"price": 200}, {"price": 100}, {"price": 200}])
        self.assertEqual(new(price_group), old(price_group))
        self.assertEqual(new(pd.DataFrame()), "")

    @patch.dict(os.environ, {"PRICE_SORT_SOURCE": "google_sheets"})
    def test_legacy_mode_keeps_formatter_byte_for_byte(self):
        old, new = self.renderers()
        for rows in (
            [{"memory": "256 GB", "color": "White", "price": 200},
             {"memory": "128 GB", "color": "Black", "price": 100}],
            [{"memory": "256 GB", "price": 200}, {"memory": "128 GB", "price": 100}],
            [{"color": "White", "price": 200}, {"color": "Black", "price": 100}],
            [{"price": 200}, {"price": 100}],
        ):
            group = self.group(rows)
            self.assertEqual(new(group), old(group))


if __name__ == "__main__":
    unittest.main()
