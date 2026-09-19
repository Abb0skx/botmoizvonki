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
3. Build `Dockerfile.worker` against the existing production base. Prepare its
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

Limits: one CPU-int8 Whisper small model plus Vosk Uzbek, pyannote batch 1,
CPU threads 2, 1.5 CPU quota, 3.5 GiB RAM, at most 512 MiB container swap. Calls longer than
180 seconds keep their complete text but deliberately skip CPU-heavy diarization;
their speakers remain unresolved. Inference is local and
model access is offline. Runtime network is needed only for downloading authorized
call recordings and updating the existing Telegram call card. Client speech is
not sent to an external ASR/LLM. Full transcript text is not printed in logs.

Hybrid routing runs both local recognizers sequentially per speaker turn: Vosk
for Uzbek and Russian-only Whisper small. This deliberately trades some speed
for avoiding whole-call language selection; arbitrary Turkish/CJK output is not
accepted. Accuracy and manager/client roles still require operator review. Unknown roles
remain speaker labels. No historical backfill runs automatically.
