# Dedicated CPU worker: 18 September 2026

Deployment-specific files, not a generic infrastructure provisioner. The web app
remains managed by Coolify; only the calls application moves to the tested feature
branch. Other projects and the repository's main branch are untouched.

The worker uses the existing production base image and separately installed,
CPU-only Python environment mounted read-only. Do not rebuild its base image or
upgrade that environment independently without a new offline smoke test.

Host directory: `/opt/texnikach-transcription-20260918` (0700). Secrets, SQLite
backup, models and sample exports stay there, not in Git. `worker.env` is 0600.
The Hugging Face download token is NOT mounted into either runtime container.

Release order:

1. Offline real-audio probe with CPU/RAM limits; consistent SQLite backup and
   `PRAGMA quick_check`.
2. `coolify_release.php` actions `inspect`, then `backup`. Export its backup from
   the Coolify container before proceeding.
3. Build `Dockerfile.worker` against the exact existing production base by
   passing `--build-arg CALLS_BASE_IMAGE=<verified image>`. Prepare its
   private environment with `prepare_worker_env.py`.
4. Push the feature commit. Run the PHP helper with `ASR_RELEASE_ACTION=deploy`
   and `ASR_RELEASE_COMMIT=<full tested SHA>`. This sets only the calls app branch
   and two transcription runtime flags through Coolify's own models and queue.
5. Verify web health and protected stats routes, then launch exactly one worker:
   `docker compose -p texnikach-transcription -f compose.worker.yaml up -d`.
6. Verify one queue result and its Telegram edit, RAM and other project health.

Pause inference: `docker compose -p texnikach-transcription -f compose.worker.yaml stop`.
The web keeps sending call cards and retains queued jobs. To disable queueing too,
set `TRANSCRIPTION_ENABLED=false` in the calls app and redeploy that app.

Rollback configuration: restore the exported Coolify backup to the helper's
expected path and run `ASR_RELEASE_ACTION=restore-config`. This does not roll back
running containers. Use the previous successful calls deployment/image in Coolify
after stopping the worker. The DB changes are additive; do NOT restore an older
DB over newer calls. Keep the SQLite backup for recovery, not routine rollback.

Current limits: CPU GigaAM v3 RNNT plus CPU-int8 Uzbek Callcenter Medium, pyannote
batch 1, CPU threads 2, 1.5 CPU quota, 4.5 GiB RAM, at most 512 MiB container swap.
The quality release raises the diarization bound from 180 to 1800 seconds,
matching the maximum accepted recording duration. Inference is local and
model access is offline. Runtime network is needed only for downloading authorized
call recordings and updating the existing Telegram call card. Client speech is
not sent to an external ASR/LLM. Full transcript text is not printed in logs.

Hybrid routing runs both local recognizers sequentially per speech window:
Uzbek Callcenter Whisper Medium and Russian GigaAM v3 RNNT. This deliberately trades some speed
for avoiding whole-call language selection; arbitrary Turkish/CJK output is not
accepted. Accuracy and manager/client roles still require operator review. Unknown roles
remain speaker labels. No historical backfill runs automatically.

## Quality release 19 September 2026

Staging/runtime extras are isolated in `/opt/texnikach-asr-quality-20260919`:
`deps`, `models`, `project`, private `results`, names-only `catalog/catalog.json`.
The original Python environment and prior worker image remain available for rollback.
`requirements-transcription-gigaam.txt` pins the upstream GigaAM source commit.
Its Hydra/OmegaConf dependencies load checkpoint configuration; SentencePiece is
required by its decoder import. Conversion utilities are not invoked by runtime.
Do not install a second CUDA torch build into this CPU-only environment.

The host timer `texnikach-asr-catalog.timer` runs every 15 minutes. Its script
opens the existing price database read-only and atomically exports product names
only. Workers mount this export read-only, not the price database or its WAL.
Prices and sales are never modified. An export failure preserves the last file.

The release preserves raw ASR words/timestamps in JSON, versions overwritten
transcripts in `call_transcription_versions`, and independently refreshes Telegram
cards every two seconds. Existing call card buttons and SMS logic are unchanged.
Retries use at most two expanded-context windows per call; they are not an LLM
rewrite or a guarantee that an alternative is correct. Confidence is uncalibrated;
GigaAM RNNT has no word confidence here, so it is stored as null, not fabricated.
CTC posterior scores are available for the optional multilingual CTC backend.

Private comparison: `compare-quality.sh gigaam <audio...>`, `hybrid-gigaam`,
`hybrid-navai`, or `baseline`. Each candidate runs with network disabled.
`baseline` means the Whisper engines with current routing, not a bit-identical
replay of the old deployed code. Never treat model-to-model agreement as WER.

`prepare_call_evaluation.py --output <private-directory> --limit 30` prepares
recordings with empty human references. `evaluate_call_asr.py` only scores
verified, nonempty references. Do not train or report accuracy on ASR-generated
pseudo-labels. Voiceprints/fine-tuning are not enabled without verified material.

Release/backfill: take a SQLite online backup first. After web health is verified,
build the new worker tag against that exact web image and start one worker with
the new compose file/private worker.env. Use `requeue_recent_transcriptions.py
--db <calls.db> --limit 10` explicitly to reprocess; it preserves previous text,
skips internal/processing calls, and sends no SMS or new Telegram posts.
