"""Explicit model provisioning; no client audio is read by this script."""
import argparse
from pathlib import Path

REVISION = "9c67dea55c8ac11f237d60ca0c32e5dc5a8c3de5"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--convert-only", action="store_true")
    args = parser.parse_args()
    from huggingface_hub import snapshot_download
    source = Path(args.source).resolve()
    if not args.convert_only:
        snapshot_download("navai-uz/whisper-medium-uzbek", revision=REVISION, local_dir=source,
                          allow_patterns=["*.json", "*.txt", "*.safetensors", "LICENSE", "NOTICE", "README.md"])
    if args.download_only:
        print("Pinned NavAI model downloaded")
        return
    if not args.output or Path(args.output).exists():
        raise SystemExit("--output must be a new directory")
    from transformers import WhisperTokenizerFast
    from ctranslate2.converters import TransformersConverter
    WhisperTokenizerFast.from_pretrained(str(source), local_files_only=True).save_pretrained(str(source))
    converter = TransformersConverter(str(source), load_as_float16=True, low_cpu_mem_usage=True,
        copy_files=sorted({p.name for p in source.glob("*.json") if p.name != "config.json"} | {"merges.txt"}))
    converter.convert(args.output, quantization="int8")
    for filename in ("config.json", "model.bin", "tokenizer.json"):
        if not Path(args.output, filename).is_file():
            raise SystemExit("Incomplete conversion: " + filename)
    print("NavAI CPU/int8 model prepared")


if __name__ == "__main__":
    main()
