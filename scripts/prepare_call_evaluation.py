"""Private evaluation set. Run in the calls container with its recording access.

References are deliberately empty. A human must listen and set verified=true
before the evaluator scores accuracy or a training exporter uses the text.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.limit <= 30:
        parser.error("limit: 1–30")
    import botmoizvonki as bot
    from call_transcription.models import private_write
    root = Path(args.output)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest_path = root / "references.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    records = {int(row['call_id']):row for row in previous}
    with bot.connect_db() as conn:
        rows = conn.execute("""SELECT id,recording,duration FROM calls
            WHERE recording IS NOT NULL AND recording != '' AND duration > 0
              AND COALESCE(is_internal_contact,0)=0 ORDER BY id DESC LIMIT ?""", (args.limit,)).fetchall()
    downloaded = 0
    for row in rows:
        call_id = row['id']
        target = root / f"{call_id}.audio"
        try:
            if not target.exists():
                bot.download_recording_limited(row['recording'], target)
                target.chmod(0o600)
                downloaded += 1
            records.setdefault(call_id,{"call_id":call_id,"audio":target.name,
                "reference":None,"verified":False,"segments":[],
                "review_fields":["words","ru_uz","product_names","prices","speaker_roles"]})
        except Exception as exc:
            if target.exists():
                target.unlink()
            print(json.dumps({"call_id":call_id,"error":type(exc).__name__}),flush=True)
    private_write(manifest_path,json.dumps(list(records.values()),ensure_ascii=False,indent=2))
    print(json.dumps({"reference_entries":len(records),"downloaded":downloaded,
                      "verified":sum(bool(r.get('verified')) for r in records.values())}))


if __name__ == '__main__':
    main()
