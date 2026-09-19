"""Offline candidate runs. Logs metrics only; private hypotheses stay in files.

Run each variant in a separate process/container to release all model memory.
No automatic production selection and no claimed WER without human references.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import resource
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from call_transcription import CallTranscriber, TranscriptionConfig
from call_transcription.models import private_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", nargs="+")
    parser.add_argument("--variant", choices=["gigaam", "hybrid-gigaam", "hybrid-navai", "baseline"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--navai-path")
    args = parser.parse_args()
    config = TranscriptionConfig.from_env()
    if args.variant == "gigaam":
        config = replace(config, backend="gigaam")
    else:
        config = replace(config, backend="hybrid", russian_engine="whisper" if args.variant == "baseline" else "gigaam")
    if args.variant == "hybrid-navai":
        if not args.navai_path:
            parser.error("--navai-path required")
        config = replace(config, uzbek_model_path=args.navai_path, uzbek_whisper_model="navai-uz/whisper-medium-uzbek")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    service = CallTranscriber(config)
    metrics = []
    for filename in args.audio:
        call_id = Path(filename).stem
        result = service.transcribe(filename, call_id=call_id)
        result.save_json(output / f"{call_id}-{args.variant}.json")
        result.save_txt(output / f"{call_id}-{args.variant}.txt")
        item = {"call_id": call_id, "variant": args.variant, "duration": result.duration,
                "seconds": result.processing_seconds,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "segments": len(result.segments), "warnings": result.warnings,
                "wer": None, "reference_status": "needs_human_transcript"}
        metrics.append(item)
        print(json.dumps(item), flush=True)
    private_write(output / f"metrics-{args.variant}.json", json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
