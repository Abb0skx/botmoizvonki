"""Prepare a minimal server worker image overlay, never edit the source files.

Run against files copied from the current production IMAGE (not a possibly
stale host checkout). SHA-256 inputs make accidental source changes fail closed.
The output only contains Price.py, Excel new.py and the standalone adapter.
Build FROM the current worker image and COPY these files into /app/Cod/.
"""
import argparse
import ast
import hashlib
from pathlib import Path


PRICE_ANCHOR = '''def load_product_sort():

    content = download_google_excel(
'''
PRICE_REPLACEMENT = '''def load_product_sort():

    source = os.getenv("PRICE_SORT_SOURCE", "google_sheets").strip().lower()
    if source == "server":
        from server_sorting_source import load_server_sorting

        return load_server_sorting(CatalogGroup, CatalogEntry)
    if source != "google_sheets":
        raise RuntimeError("Unknown PRICE_SORT_SOURCE; publication stopped without fallback")

    content = download_google_excel(
'''
EXCEL_OPEN_ANCHOR = '''print(
    "⬇️ Загружаю product_sort из Google Sheets..."
)

sort_spreadsheet = gc.open_by_key(
    SORT_SPREADSHEET_ID
)
'''
EXCEL_OPEN_REPLACEMENT = '''sort_source = os.getenv("PRICE_SORT_SOURCE", "google_sheets").strip().lower()
if sort_source == "google_sheets":
    print("⬇️ Загружаю product_sort из Google Sheets...")
    sort_spreadsheet = gc.open_by_key(SORT_SPREADSHEET_ID)
elif sort_source == "server":
    print("⬇️ Загружаю сортировку с сайта...")
    sort_spreadsheet = None
else:
    raise RuntimeError("Unknown PRICE_SORT_SOURCE; export stopped without fallback")
'''
EXCEL_ROWS_ANCHOR = '''(
    sort_worksheet,
    sort_values,
    sort_headers,
) = worksheet_with_columns(
    sort_spreadsheet,
    required_sort_columns,
)
'''
EXCEL_ROWS_REPLACEMENT = '''if sort_source == "server":
    from server_sorting_source import load_server_sorting_rows

    sort_worksheet, sort_values, sort_headers = load_server_sorting_rows()
else:
    (
        sort_worksheet,
        sort_values,
        sort_headers,
    ) = worksheet_with_columns(
        sort_spreadsheet,
        required_sort_columns,
    )
'''
FORMAT_SORT_BLOCK = '''            group
            .sort_values(
                by="price"
            )
'''
FORMAT_DETAILS_ANCHOR = '''    details = []

    model_name = (
'''
FORMAT_DETAILS_REPLACEMENT = '''    if os.getenv("PRICE_SORT_SOURCE", "google_sheets").strip().lower() == "server":
        from server_sorting_source import sort_price_rows

        sorted_group = sort_price_rows(group)
    else:
        sorted_group = group.sort_values(by="price")

    details = []

    model_name = (
'''


def _replace_once(source, old, new):
    if source.count(new) == 1 and old not in source:
        return source
    if source.count(old) != 1 or new in source:
        raise ValueError("Worker source differs from the reviewed anchors; aborting")
    return source.replace(old, new, 1)


def patch_price(source):
    source = _replace_once(source, PRICE_ANCHOR, PRICE_REPLACEMENT)
    source = patch_format_prices(source)
    ast.parse(source)
    return source


def patch_format_prices(source):
    """Change ordering only inside the exact existing formatting function."""
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "format_prices"]
    if len(functions) != 1:
        raise ValueError("Worker format_prices function changed; aborting")
    function = functions[0]
    lines = source.splitlines(keepends=True)
    original = "".join(lines[function.lineno - 1:function.end_lineno])
    if (original.count(FORMAT_DETAILS_REPLACEMENT) == 1
            and original.count("            sorted_group\n            .groupby(") == 4
            and FORMAT_SORT_BLOCK not in original):
        return source
    if original.count(FORMAT_SORT_BLOCK) != 4 or FORMAT_DETAILS_REPLACEMENT in original:
        raise ValueError("Worker format_prices ordering changed; aborting")
    changed = _replace_once(original, FORMAT_DETAILS_ANCHOR, FORMAT_DETAILS_REPLACEMENT)
    changed = changed.replace(FORMAT_SORT_BLOCK, "            sorted_group\n")
    lines[function.lineno - 1:function.end_lineno] = [changed]
    result = "".join(lines)
    ast.parse(result)
    return result


def patch_excel(source):
    source = _replace_once(source, EXCEL_OPEN_ANCHOR, EXCEL_OPEN_REPLACEMENT)
    source = _replace_once(source, EXCEL_ROWS_ANCHOR, EXCEL_ROWS_REPLACEMENT)
    ast.parse(source)
    return source


def prepare(source_dir, output_dir, price_sha256, excel_sha256):
    source_dir, output_dir = Path(source_dir).resolve(), Path(output_dir).resolve()
    if source_dir == output_dir or source_dir in output_dir.parents:
        raise ValueError("Output must be separate from the worker source directory")
    inputs = {}
    for filename, expected, transform in (
        ("Price.py", price_sha256, patch_price),
        ("Excel new.py", excel_sha256, patch_excel),
    ):
        content = (source_dir / filename).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("Worker source checksum differs; aborting")
        inputs[filename] = transform(content.decode("utf-8"))
    adapter = Path(__file__).resolve().parents[1] / "price_server" / "worker_sorting.py"
    inputs["server_sorting_source.py"] = adapter.read_text(encoding="utf-8")
    for filename, content in inputs.items():
        target = output_dir / filename
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise ValueError("Output already contains different files; use a new output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in inputs.items():
        target = output_dir / filename
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    return {filename: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for filename, content in inputs.items()}


if __name__ == "__main__":
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--price-sha256", required=True)
    parser.add_argument("--excel-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source_dir, args.output_dir, args.price_sha256,
                             args.excel_sha256), sort_keys=True))
