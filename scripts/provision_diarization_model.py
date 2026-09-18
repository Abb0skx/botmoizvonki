"""Explicit online download; no recordings, no token in argv/environment/logs."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    from huggingface_hub import HfApi, snapshot_download
    token = Path(args.token_file).read_text().strip()
    repo = "pyannote/speaker-diarization-community-1"
    info = HfApi().model_info(repo, token=token, files_metadata=True)
    size = sum(item.size or 0 for item in info.siblings)
    if size > 1024 ** 3:
        raise RuntimeError("Model download exceeds the 1 GiB provisioning budget")
    print(json.dumps({"repository": repo, "revision": info.sha, "bytes": size}), flush=True)
    snapshot_download(repo, revision=info.sha, local_dir=args.directory,
                      token=token, max_workers=1)
    print("Local diarization model downloaded", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("Model provisioning failed: " + type(exc).__name__) from None
