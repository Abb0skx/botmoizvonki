"""Offline browser regression check; never contacts production or Telegram.

Run with a Python environment containing playwright and a Chromium browser:
  python scripts/check_price_models_ui.py --chromium /path/to/chrome
Optional --screenshots /tmp/models-ui saves desktop/mobile visual evidence.
All browser requests are intercepted; mutations use a temporary SQLite DB.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright, expect

from price_server.entry_catalog import EntryCatalogService, install_catalog
from price_server.entry_store import initialize
from price_server.price_entry import EntryError, HEADERS, identity, revision


def check(chromium=None, screenshots=None):
    with tempfile.TemporaryDirectory(prefix="models-ui-") as folder, sync_playwright() as pw:
        db = Path(folder) / "fixture.db"
        row = dict(zip(HEADERS, [1, "Existing phone", "Black", "256 GB", 10, 100, 11, None, 95, "Телефоны Samsung"]))
        row.update(key=identity(row), row=2, locked=[], revision=revision(row))
        initialize(db, [{"sheet_id": 1, "title": "1-Fixture", "rows": [row]}])
        install_catalog(db, [{"category_id": 10, "name": "Телефоны Samsung"},
                             {"category_id": 26, "name": "Часы Apple Watch"}],
                        max_product_id=100, max_version_id=200)
        catalog = EntryCatalogService(db)
        fault = {"mode": None}
        writes, browser_errors = [], []
        browser = pw.chromium.launch(**({"executable_path": chromium} if chromium else {}))
        context = browser.new_context(viewport={"width": 1440, "height": 1150}, reduced_motion="reduce")

        def handle(route):
            request = route.request
            path = urlsplit(request.url).path
            if path == "/price/models":
                route.fulfill(path=str(ROOT / "price_server/static/price-models.html"), content_type="text/html")
                return
            if path.startswith("/price/assets/"):
                asset = ROOT / "price_server/static" / Path(path).name
                route.fulfill(path=str(asset), content_type="text/css" if asset.suffix == ".css" else "application/javascript")
                return
            prefix = "/monitoring/api/prices/admin/entry/"
            if not path.startswith(prefix):
                route.abort()
                return
            endpoint = path.removeprefix(prefix)
            try:
                if request.method == "GET":
                    result = catalog.models() if endpoint == "models" else catalog.categories() if endpoint == "categories" else catalog.model(int(endpoint.split("/")[-1]))
                else:
                    body = request.post_data_json
                    if endpoint == "model-import/preview":
                        result = catalog.preview_import(body["text"], body["category_id"])
                    else:
                        operation = request.headers["idempotency-key"]
                        writes.append(operation)
                        if fault["mode"] == "auth":
                            fault["mode"] = None
                            route.fulfill(status=401, json={"detail": "session_expired"})
                            return
                        result = catalog.create(body, operation) if endpoint == "products" else catalog.import_models(body["text"], body["category_id"], body["preview_hash"], operation)
                        if fault["mode"] == "lost_response":
                            fault["mode"] = None
                            route.abort("failed")
                            return
                route.fulfill(json=result)
            except EntryError as error:
                route.fulfill(status=error.status, json={"detail": {"code": error.code, **error.details}})

        context.route("**/*", handle)
        page = context.new_page()
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.set_default_timeout(7000)
        page.goto("http://127.0.0.1:8765/price/models")
        expect(page.locator("#workspace")).to_be_visible()
        category = page.locator("#import-category-search")
        category.fill("Apple Watch")
        category.press("ArrowDown")
        category.press("Enter")
        expect(page.locator("#import-category-selected")).to_contain_text("Выбрано: Часы Apple Watch")
        page.locator("#new-model-name").fill("Apple Watch Fixture")
        page.locator("#new-memories").fill("42mm, 46mm")
        page.locator("#new-colors").fill("Black, Silver")
        expect(page.locator("#single-count")).to_have_text("4")
        page.locator("#single-variants input").last.uncheck()
        expect(page.locator("#single-count")).to_have_text("3")
        page.reload()
        expect(page.locator("#single-count")).to_have_text("3")
        expect(page.locator("#new-model-name")).to_have_value("Apple Watch Fixture")

        def snapshot(name, width):
            page.set_viewport_size({"width": width, "height": 1150 if width > 800 else 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"Horizontal overflow at {width}px"
            if screenshots:
                target = Path(screenshots)
                target.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target / (name + ".png")), full_page=True)

        for width in (1440, 768, 390):
            snapshot(f"single-{width}", width)
        page.set_viewport_size({"width": 1440, "height": 1150})
        page.locator("#preview-import").click()
        expect(page.locator("#preview-summary")).to_contain_text("конфигураций: 3")
        expect(page.locator(".review-chip")).to_have_count(3)
        snapshot("review", 1440)
        page.locator("#apply-import").click()
        expect(page.locator("#create-success")).to_be_visible()
        expect(page.locator("#success-detail")).to_contain_text("новых конфигураций: 3")
        assert catalog.models()["variant_count"] == 4
        page.locator("#create-another").click()
        page.locator("#batch-tab").click()
        page.locator("#insert-example").click()
        for width in (1440, 390):
            snapshot(f"batch-{width}", width)
        page.locator("#preview-import").click()
        expect(page.locator("#preview-summary")).to_contain_text("моделей: 2 · конфигураций: 8")
        page.locator("#reset-import").click()

        # A final Excel row with two blank cells must survive transport intact.
        page.locator("#import-text").fill("Model\tMemory\tColors\nFixture accessory\t\t")
        page.locator("#preview-import").click()
        expect(page.locator("#preview-summary")).to_contain_text("конфигураций: 1")
        fault["mode"] = "lost_response"
        page.locator("#apply-import").click()
        expect(page.locator("#apply-import")).to_have_text("Проверить результат и завершить")
        saved_operation = writes[-1]
        assert catalog.models()["variant_count"] == 5
        page.reload()
        expect(page.locator("#apply-import")).to_have_text("Проверить результат и завершить")
        fault["mode"] = "auth"
        page.locator("#apply-import").click()
        expect(page.locator("#login-panel")).to_be_visible()
        stored = page.evaluate("JSON.parse(sessionStorage.getItem('texnikach:model-creator:v1'))")
        assert stored["pending"]["operation"] == saved_operation
        page.reload()
        expect(page.locator("#apply-import")).to_have_text("Проверить результат и завершить")
        page.locator("#apply-import").click()
        expect(page.locator("#create-success")).to_be_visible()
        assert writes[-3:] == [saved_operation] * 3
        assert catalog.models()["variant_count"] == 5, "Uncertain retry created duplicates"

        page.locator("#create-another").click()
        page.locator("#import-text").fill("Fixture accessory | - | -")
        page.locator("#preview-import").click()
        expect(page.locator("#notice")).to_contain_text("Уже в каталоге: Fixture accessory")
        page.locator("#import-text").fill("Model | Memory | Colors\nBad | 128")
        page.locator("#preview-import").click()
        expect(page.locator("#notice")).to_contain_text("Строка: 2")
        page.locator("#models button").first.click()
        expect(page.locator("#dialog-title")).to_have_text("Редактировать модель")
        expect(page.locator("#dialog-actions")).to_contain_text("Удалить модель")
        assert not browser_errors, browser_errors
        print(json.dumps({"result": "passed", "checks": ["keyboard category selection", "exact combinations and exclusions", "draft restore", "single apply", "bulk blocks preview", "Excel blank cells", "lost response + reload + expired session + idempotent retry", "duplicate and line errors", "existing model edit", "no horizontal overflow: 390/768/1440"], "fixture_variants": 5}, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium")
    parser.add_argument("--screenshots")
    args = parser.parse_args()
    check(args.chromium, args.screenshots)
