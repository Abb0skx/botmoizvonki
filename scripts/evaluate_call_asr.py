"""Score private, human-verified references. Never infer accuracy from fluency.

Manifest: [{"call_id": 2470, "reference": "...", "verified": true}].
Unverified entries are excluded and reported. Reference text is never logged.
"""
import argparse
import json
from pathlib import Path
import re


def words(text):
    text = text.casefold().replace("ё", "е")
    return re.findall(r"[^\W_]+(?:['’‘ʻʼ][^\W_]+)*", text)


def distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for i, word in enumerate(reference, 1):
        new = [i]
        for j, other in enumerate(hypothesis, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (word != other)))
        row = new
    return row[-1]


def evaluate(manifest, directory, variant):
    errors = total = scored = skipped = 0
    for item in manifest:
        if not item.get("verified") or not isinstance(item.get("reference"), str):
            skipped += 1
            continue
        path = Path(directory, f"{int(item['call_id'])}-{variant}.json")
        if not path.exists():
            skipped += 1
            continue
        hypothesis = json.loads(path.read_text(encoding="utf-8"))["full_text"]
        ref, hyp = words(item['reference']), words(hypothesis)
        if not ref:
            skipped += 1
            continue
        errors += distance(ref, hyp)
        total += len(ref)
        scored += 1
    return {"variant": variant, "scored_calls": scored, "skipped_calls": skipped,
            "word_errors": errors, "reference_words": total,
            "wer": errors / total if total else None,
            "requires_human_references": total == 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("results")
    parser.add_argument("--variant", required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(Path(args.manifest).read_text()), args.results, args.variant)))
