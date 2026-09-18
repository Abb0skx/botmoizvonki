# Local transcription: server trial, 18 September 2026

## Outcome

Installed in **staging**, not activated for incoming calls. The combined pipeline
did not fit the safe test budget on the existing 4 GB VPS. Do not mark this as
a successful production transcription deployment or a verified-quality transcript.

Production bot, SQLite schema/configuration and Telegram posts were unchanged.
The call card still goes out through the existing code; no transcript was posted.

## Installed assets

- Host: `130.17.24.84`, staging directory `/opt/texnikach-transcription-20260918` (0700).
- Reusable module and probe scripts: `project/`; separate CPU environment: `venv/`.
- Existing Whisper weights reused read-only from
  `/opt/texnikach-transcription-probe-20260918/whisper` (about 1.6 GB on disk).
- Community-1 model stored in `models/diarization` (33,695,573 download bytes),
  revision `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`.
- Runtime base: existing Python 3.11.16 image
  `nylgfmvjodgie9dga7ngprgl:59c26a7fc0c76f29d00fbdbcfcc5d97b10d457c5`.
- Installed faster-whisper 1.2.1, CTranslate2 4.8.2, pyannote.audio 4.0.7,
  torch 2.8.0+cpu, torchaudio 2.8.0+cpu, torchcodec 0.7.0.
  CPU wheel setup follows [PyTorch instructions](https://pytorch.org/get-started/previous-versions/);
  version pairing follows the [TorchCodec compatibility table](https://github.com/meta-pytorch/torchcodec).
- `requirements-transcription-cpu.txt` preserves the CPU-specific direct pins.
  Transitive dependencies are not completely locked; preserve this staged environment.

The venv was created with system-site-packages from the exact base image;
install commands wrote only into the private staging venv. They did not install
packages into any running service. Model download used an explicitly mounted
read-only HF token file; inference containers never received the token.

## Measurements

Host before trial: 3,915 MiB total RAM, ~2,022 MiB MemAvailable, 6.5 GB free disk.
Inference CPU quota: 0.5 CPU, one compute thread; network disabled.

| Probe | Limits | Result |
|---|---|---|
| Whisper int8 load | 1,280 MiB RAM, no swap | cgroup OOM, exit 137 |
| Whisper int8 load | 1,536 MiB RAM + 256 MiB swap | success, 22.91 s; peak process RSS 1,561,416 KiB |
| Full 30-second call | 1,536 MiB RAM + 256 MiB swap | diarization returned 19 turns / 2 speaker IDs; then cgroup OOM loading ASR |
| ASR alone, first 20 seconds | same limits | stopped by host-reserve guard, exit 143, no completed result |

Diarization peak process RSS was 709,780 KiB. This is a peak, not a retained
allocation measurement. It must not be added blindly to other nonconcurrent
peaks to infer a precise minimum. Two returned speaker IDs are not proof of
correct speaker/role attribution: quality was not manually verified.

The host guard stopped only the probe if MemAvailable dropped below 400 MiB or
SwapFree below 200 MiB. Its ASR-alone log records the guard trigger, but not which
individual threshold triggered it. Docker OOM checks distinguish that stop from
the full pipeline's confirmed cgroup OOM. No host-wide OOM claim is made.

No full dialogue/JSON was produced. No hallucinated or placeholder text was
substituted. This trial proves the current constrained deployment is not ready,
not that every possible implementation is impossible on a 4 GB machine.

## Code and tests

- Added `LOCAL_DIARIZATION_BATCH_SIZE`, validated 1–64, default 32.
  Trial used 1 for both segmentation and embeddings, keeping the same weights
  and thresholds. Models remain cached per process as before.
- Added private model provisioning and recording-download probe scripts,
  and an offline benchmark wrapper that prints timings/counts, not speech.
- 29 transcription unit tests passed.
- Full project suite: **715 tests passed** using the project's existing venv
  (`/Users/abbos/PycharmProjects/botmoizvonki/venv/bin/python`).
  System Python had an unrelated conflicting `telegram` installation; rerunning
  in the correct project environment resolved the test-import failures.

## Privacy and cleanup

One selected 30-second call was fetched directly from the existing approved
recording provider to a 0600 staging file. Processing used `--network=none`;
no audio or transcript was sent to cloud ASR or an external LLM. The probe did
not have a database mount or Telegram credentials. The earlier download helper
mounted SQLite read-only and fetched only the selected recording URL.

The 98,224-byte temporary recording copy was removed after tests. The provider's
original recording and all call data are unchanged. Exited test containers/logs
were retained for diagnosis; they use no active process RAM. No model cache,
working database, Docker image, or unrelated project was deleted.

Final checks: dashboard HTTPS 200; calls bot, OCR and sales-photo bot had zero
automatic restarts and unchanged start times during this trial. No running
transcription probe or installation container remained. Final host snapshot:
2,270 MiB MemAvailable, swap 1,677/2,047 MiB used; disk ~4.8 GB free.

## Next step

Increasing total RAM to **6 GB** is a reasonable next trial, not a guarantee.
Repeat the full offline call test with an explicit worker budget while keeping
headroom for OCR and other projects. Measure active ASR + OCR coexistence,
review the actual RU/UZ dialogue/roles, then roll out the existing web integration
and a single external worker with the shared local SQLite volume. Only then enable
automatic jobs and verify editing the same Telegram message.

No service is scheduled to start automatically after a RAM upgrade. A new
explicit deployment/verification step is still required.
