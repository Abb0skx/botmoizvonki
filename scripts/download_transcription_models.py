"""Explicit ONLINE provisioning only. Never accepts or processes client audio."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Download local models after accepting pyannote HF conditions")
    parser.add_argument("--backend", required=True, choices=["mlx", "faster-whisper", "hybrid"])
    parser.add_argument("--directory", default="models")
    parser.add_argument("--model", choices=["small", "medium", "large-v3", "large-v3-turbo"], default="large-v3-turbo")
    args = parser.parse_args()
    from huggingface_hub import snapshot_download
    root = Path(args.directory).resolve()
    if args.backend == "mlx":
        repo = f"mlx-community/whisper-{args.model}"
    else:
        repo = {
            "small": "Systran/faster-whisper-small",
            "medium": "Systran/faster-whisper-medium",
            "large-v3": "Systran/faster-whisper-large-v3",
            "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        }[args.model]
    # Token stays in the environment/HF credential store, never command line or logs.
    token = os.getenv("HF_TOKEN") or None
    snapshot_download(repo, local_dir=root / "whisper", token=token)
    snapshot_download("pyannote/speaker-diarization-community-1", local_dir=root / "diarization", token=token)
    if args.backend == "hybrid":
        print("Whisper/diarization downloaded. Also provision official Vosk Uzbek model and set LOCAL_VOSK_MODEL_PATH.")
    print("Models downloaded. Disconnect the network and run the benchmark before activation.")


if __name__ == "__main__":
    main()
