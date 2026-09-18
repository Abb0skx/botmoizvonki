"""Real inference benchmark; never logs customer text or uploads recordings."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from call_transcription import CallTranscriber
from call_transcription.models import timestamp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args()
    result = CallTranscriber().transcribe(args.audio)
    if args.json_path:
        result.save_json(args.json_path)
    print(f"Audio duration: {timestamp(result.duration)}")
    print(f"Processing: {timestamp(result.processing_seconds)}")
    print(f"Realtime factor: {result.processing_seconds / result.duration:.2f}x")
    print(f"Backend: {result.backend}\nModel: {result.model}")
    print(f"Speakers: {len(set(result.speakers) - {'SPEAKER_UNKNOWN'})}\nSegments: {len(result.segments)}")


if __name__ == "__main__":
    main()
