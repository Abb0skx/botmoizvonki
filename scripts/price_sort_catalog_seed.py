"""Read Catalog cell rows with the worker's exact existing pure validation.

No Price.py module import, main code, environment loading, network request or
publication is executed. Only the explicitly listed classes/functions/constants
are selected from a trusted local worker source file. The file is never edited.
"""
import ast
import contextlib
import dataclasses
import html
import io
import json
import re
import types
import unicodedata
from pathlib import Path


def catalog_groups_from_rows(rows, price_source_path):
    import pandas as pd
    from bs4 import BeautifulSoup

    if not isinstance(rows, list) or not rows or not all(isinstance(row, list) for row in rows):
        raise ValueError("Catalog rows must include a header and row arrays")
    selected = {
        "CatalogEntry", "CatalogGroup", "CATALOG_REQUIRED_COLUMNS", "CATALOG_WORKSHEET_NAME",
        "clean_catalog_value", "parse_catalog_enabled", "parse_catalog_order",
        "build_catalog_notice_html", "is_catalog_emoji_character", "load_catalog_groups",
        "serialize_catalog_groups_for_server",
    }
    source = Path(price_source_path).read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(price_source_path))
    nodes = []
    found = set()
    for node in module.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in selected:
            nodes.append(node)
            found.add(name)
    if selected != found:
        raise ValueError("Worker Catalog parser changed; review migration before continuing")

    def read_excel(_file, *, header=0, nrows=None, **_kwargs):
        if header is None:
            return pd.DataFrame(rows[:nrows] if nrows is not None else rows)
        width = len(rows[0])
        values = [(row + [""] * width)[:width] for row in rows[1:]]
        return pd.DataFrame(values[:nrows] if nrows is not None else values, columns=rows[0])

    namespace = {
        "__name__": __name__, "dataclass": dataclasses.dataclass, "html": html,
        "pd": types.SimpleNamespace(isna=pd.isna, read_excel=read_excel),
        "re": re, "unicodedata": unicodedata, "BeautifulSoup": BeautifulSoup,
    }
    code = compile(ast.Module(body=nodes, type_ignores=[]), str(price_source_path), "exec")
    exec(code, namespace)
    with contextlib.redirect_stdout(io.StringIO()):
        groups = namespace["load_catalog_groups"](types.SimpleNamespace(sheet_names=["Catalog"]))
    return namespace["serialize_catalog_groups_for_server"](groups)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("price_source", type=Path)
    parser.add_argument("rows_json", type=Path)
    args = parser.parse_args()
    rows = json.loads(args.rows_json.read_text(encoding="utf-8"))
    json.dump(catalog_groups_from_rows(rows, args.price_source), sys.stdout,
              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
