"""Download and convert the pinned Uzbek call-centre model to CTranslate2/int8.

This is an explicit ONLINE provisioning step. It never reads client audio and
must not run inside the production worker. The runtime receives only the
converted directory and works with Hugging Face offline mode enabled.
"""

from __future__ import annotations

import argparse
from pathlib import Path


MODEL_ID = "Abduqayum/whisper-uzbek-medium-callcenter"
MODEL_REVISION = "0eed53663594a96292e1beaaaa0b2bdbea1a157e"
COPY_FILES = [
    "added_tokens.json",
    "generation_config.json",
    "merges.txt",
    "normalizer.json",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
]


def main():
    parser = argparse.ArgumentParser(
        description="Prepare the pinned Uzbek telephone Whisper model for CPU/int8 inference"
    )
    parser.add_argument(
        "--output-dir",
        default="models/whisper-uzbek-callcenter-medium",
        help="A new directory for the converted model",
    )
    args = parser.parse_args()

    output = Path(args.output_dir).expanduser().resolve()
    if output.exists():
        raise SystemExit(
            f"Refusing to overwrite existing path: {output}. "
            "Move it aside and rerun if a fresh conversion is required."
        )
    output.parent.mkdir(parents=True, exist_ok=True)

    from ctranslate2.converters import TransformersConverter

    converter = TransformersConverter(
        MODEL_ID,
        revision=MODEL_REVISION,
        copy_files=COPY_FILES,
        load_as_float16=True,
        low_cpu_mem_usage=True,
    )
    converter.convert(str(output), quantization="int8")

    required = (output / "model.bin", output / "config.json", output / "tokenizer.json")
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Conversion is incomplete; missing: " + ", ".join(missing))

    print(f"Prepared pinned Uzbek Callcenter CTranslate2/int8 model in {output}")


if __name__ == "__main__":
    main()
