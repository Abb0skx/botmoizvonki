"""Bounded deployment probe. Run in a network-disabled resource-limited container.

Never prints client speech, audio, telephone numbers, or recording URLs.
Private JSON/TXT stay on the server; operator reviews them separately.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import resource
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from call_transcription import CallTranscriber, TranscriptionConfig
from call_transcription.audio import prepared_audio
from call_transcription.asr.faster_whisper_backend import FasterWhisperBackend
from call_transcription.diarization import PyannoteDiarizer


def emit(stage, **values):
    print(json.dumps({"stage": stage,
                      "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      **values}), flush=True)


class Measured:
    def __init__(self, inner):
        self.inner = inner

    def diarize(self, *args, **kwargs):
        emit("diarization_start")
        result = self.inner.diarize(*args, **kwargs)
        emit("diarization_done", turns=len(result), speakers=len({t.speaker for t in result}))
        return result

    def transcribe(self, *args, **kwargs):
        emit("asr_window_start", start=kwargs.get("start"), end=kwargs.get("end"))
        result = self.inner.transcribe(*args, **kwargs)
        emit("asr_window_done", segments=len(result))
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=["full", "diarize", "asr"], default="full")
    args = parser.parse_args()
    config = TranscriptionConfig.from_env()
    config.check_model_paths()
    started = time.monotonic()
    emit("probe_start", mode=args.mode)
    diarizer = Measured(PyannoteDiarizer(config))
    backend = Measured(FasterWhisperBackend(config))
    if args.mode == "full":
        result = CallTranscriber(config, backend=backend, diarizer=diarizer).transcribe(args.audio)
        result.save_json(args.output + ".json")
        result.save_txt(args.output + ".txt")
        emit("complete", duration=result.duration, seconds=time.monotonic() - started,
             segments=len(result.segments), speakers=len(result.speakers),
             languages=dict(Counter(s.language for s in result.segments)),
             role_resolution=result.role_resolution, warnings=result.warnings)
    else:
        with prepared_audio(args.audio, config) as (path, duration):
            if args.mode == "diarize":
                diarizer.diarize(path)
            else:
                from call_transcription.processing import build_prompt
                backend.transcribe(path, start=0, end=min(duration, 20), initial_prompt=build_prompt([]))
        emit("complete", seconds=time.monotonic() - started)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Cause TYPES help identify dependency/API errors, without recording text.
        chain, current = [], exc
        while current is not None and len(chain) < 6:
            chain.append(type(current).__name__)
            current = current.__cause__
        emit("error", errors=chain, frames=[
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ])
        raise SystemExit(1) from None
