"""Export names only; source prices database is opened read-only."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from call_transcription.catalog import load_catalog
from call_transcription.models import private_write


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    terms, aliases = load_catalog(args.db)
    if not terms:
        raise SystemExit("No catalogue terms: keeping the previous export")
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_write(target, json.dumps({"terms":terms,"aliases":aliases,"updated_at":int(time.time())},ensure_ascii=False))
    print(json.dumps({"catalog_terms":len(terms)}))
