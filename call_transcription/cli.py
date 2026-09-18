import argparse
import logging
import sys
from dataclasses import replace

from .config import TranscriptionConfig
from .errors import TranscriptionError
from .transcriber import CallTranscriber


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локальная RU/UZ расшифровка (без облачных API)")
    parser.add_argument("audio")
    parser.add_argument("--json", dest="json_path")
    parser.add_argument("--txt", dest="txt_path")
    parser.add_argument("--backend", choices=["auto", "mlx", "faster-whisper"])
    parser.add_argument("--model-path")
    parser.add_argument("--diarization-model-path")
    parser.add_argument("--term", action="append", default=[])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = TranscriptionConfig.from_env()
        changes = {k: v for k, v in {
            "backend": args.backend, "model_path": args.model_path,
            "diarization_model_path": args.diarization_model_path,
        }.items() if v is not None}
        result = CallTranscriber(replace(config, **changes)).transcribe(args.audio, context_terms=args.term)
        if args.json_path:
            result.save_json(args.json_path)
        if args.txt_path:
            result.save_txt(args.txt_path)
        # Explicit CLI output, not a background/customer-text log.
        print(result.to_txt(), end="")
        return 0
    except (TranscriptionError, FileNotFoundError, OSError) as exc:
        print(f"Ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
