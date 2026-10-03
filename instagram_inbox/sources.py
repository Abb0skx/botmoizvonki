"""Read-only, bounded Google Sheets input. Never serve an expired price cache."""
import asyncio
import csv
import io
import re
import time
from decimal import Decimal, InvalidOperation

import httpx
from telegram_business.products import ExistingGoogleProductRepository, ProductVariant


def rows_from_csv(value, required):
    reader = csv.DictReader(io.StringIO(value.lstrip('\ufeff')))
    headers = [str(h or '').strip().lower() for h in reader.fieldnames or []]
    if len(set(headers)) != len(headers) or not set(required).issubset(headers):
        raise ValueError('Invalid sheet headers')
    return [{str(k or '').strip().lower(): str(v or '').strip() for k, v in row.items()}
            for row in reader if any(row.values())]


def decimal(value):
    try:
        number = Decimal(str(value).replace(' ', '').replace('\u00a0', '').replace(',', '.'))
    except InvalidOperation:
        raise ValueError('Invalid price or exchange rate') from None
    if not number.is_finite() or number <= 0:
        raise ValueError('Invalid price or exchange rate')
    return number


class ProductIndex(ExistingGoogleProductRepository):
    """Reuse the tested RU/UZ model/memory/color matcher, not its network adapter."""
    def __init__(self, variants):
        super().__init__(max_age_minutes=5)
        self._variants = variants
        self._loaded = time.time()

    def _load(self):
        pass  # The asynchronous source owns fetching and freshness.


def parse_catalog(prices, settings):
    rate_rows = rows_from_csv(settings, {'setting', 'value'})
    rates = [row['value'] for row in rate_rows if row['setting'] == 'kurs']
    if len(rates) != 1:
        raise ValueError('Exactly one exchange rate is required')
    rate = decimal(rates[0])
    products = rows_from_csv(prices, {'product_id', 'model_name', 'memory', 'color', 'price'})
    variants = []
    seen = set()
    for row in products:
        if not row['model_name'] or not row['price']:
            continue
        product_id = int(row['product_id'])
        warranty = int(row['warranty_period']) if row.get('warranty_period') else None
        identity = (product_id, row['model_name'], row['memory'], row['color'], warranty)
        if product_id <= 0 or identity in seen or (warranty is not None and warranty <= 0):
            raise ValueError('Duplicate or invalid product id')
        seen.add(identity)
        variants.append(ProductVariant(model=row['model_name'], memory=row['memory'], color=row['color'],
            price_uzs=decimal(row['price']) * rate, product_id=product_id, warranty_months=warranty))
    if not variants:
        raise ValueError('Empty product catalog')
    return ProductIndex(variants), rate


def parse_config(rules_csv, settings_csv):
    settings = {}
    for row in rows_from_csv(settings_csv, {'setting', 'value'}):
        key = row['setting']
        if not key or key in settings:
            raise ValueError('Duplicate or empty template setting')
        settings[key] = row['value']
    rules = []
    for index, row in enumerate(rows_from_csv(rules_csv,
            {'priority', 'enabled', 'keywords', 'match_type', 'reply_text'}), 2):
        if row['enabled'].lower() not in ('true', '1', 'yes', 'on', 'да'):
            continue
        kind = row['match_type'].lower()
        keywords = [v.strip() for v in re.split(r'[,;\n]+', row['keywords']) if v.strip()]
        if kind not in ('contains_any', 'contains_all', 'exact', 'default'):
            raise ValueError('Unknown rule type')
        if not row['reply_text'] or (kind != 'default' and not keywords):
            raise ValueError('Empty enabled rule')
        rules.append(dict(priority=int(row['priority'] or 0), keywords=keywords,
            match_type=kind, reply_text=row['reply_text'], row_number=index))
    if not any(r['match_type'] == 'default' for r in rules):
        raise ValueError('Default reply must be configured')
    return {'settings': settings, 'rules': sorted(rules, key=lambda r: -r['priority'])}


class SheetSource:
    def __init__(self, settings, http=None):
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=15, follow_redirects=True)
        self.cache, self.loaded, self.locks, self.errors = {}, {}, {}, {}

    async def csv(self, spreadsheet, sheet, width):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', spreadsheet):
            raise ValueError('Invalid spreadsheet id')
        async with self.http.stream('GET', f'https://docs.google.com/spreadsheets/d/{spreadsheet}/gviz/tq',
                params={'tqx': 'out:csv', 'sheet': sheet, 'range': f'A1:{width}10000'}) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError('Sheet exceeds size limit')
            return data.decode('utf-8-sig')

    async def get(self, kind, force=False):
        async with self.locks.setdefault(kind, asyncio.Lock()):
            age = time.monotonic() - self.loaded.get(kind, 0)
            if not force and not self.errors.get(kind) and kind in self.cache and age < self.settings.sheets_cache_seconds:
                return self.cache[kind]
            try:
                cfg = self.settings
                if kind == 'config':
                    a, b = await asyncio.gather(
                        self.csv(cfg.rules_sheet_id, cfg.direct_rules_sheet_name, 'F'),
                        self.csv(cfg.rules_sheet_id, cfg.direct_settings_sheet_name, 'C'))
                    result = parse_config(a, b)
                elif kind == 'catalog':
                    a, b = await asyncio.gather(
                        self.csv(cfg.products_sheet_id, cfg.products_sheet_name, 'F'),
                        self.csv(cfg.products_sheet_id, cfg.product_settings_sheet_name, 'C'))
                    result = parse_catalog(a, b)
                else:
                    raise ValueError('Unknown sheet source')
            except Exception:
                self.errors[kind] = True
                # Do not reset loaded time or silently return old prices after a failed refresh.
                raise
            self.cache[kind], self.loaded[kind] = result, time.monotonic()
            self.errors[kind] = False
            return result

    def status(self):
        return {kind: ('unavailable' if self.errors.get(kind) else
                      'fresh' if kind in self.loaded and time.monotonic() - self.loaded[kind] < self.settings.sheets_cache_seconds
                      else 'not_loaded_or_expired') for kind in ('config', 'catalog')}

    async def close(self):
        await self.http.aclose()
