#!/bin/sh
# Deployment-local offline smoke comparison, one candidate at a time.
set -eu
variant=${1:?Pass gigaam, hybrid-gigaam, hybrid-navai or baseline}
shift
docker run --rm --name texnikach-quality-compare --network none \
  --memory 4608m --memory-swap 5120m --cpus 1.5 \
  -e PYTHONPATH=/quality/deps:/quality/project \
  -e LOCAL_TRANSCRIPTION_DEVICE=cpu -e LOCAL_TRANSCRIPTION_COMPUTE_TYPE=int8 \
  -e LOCAL_TRANSCRIPTION_CPU_THREADS=2 -e LOCAL_DIARIZATION_BATCH_SIZE=1 \
  -e LOCAL_DIARIZATION_MAX_DURATION_SECONDS=1800 -e LOCAL_TRANSCRIPTION_SPEECH_GAP=1.5 \
  -e LOCAL_GIGAAM_MODEL_PATH=/quality/models \
  -e LOCAL_DIARIZATION_MODEL_PATH=/diarization \
  -e LOCAL_WHISPER_MODEL=small -e LOCAL_WHISPER_MODEL_PATH=/models/whisper-small \
  -e LOCAL_UZBEK_WHISPER_MODEL_PATH=/models/whisper-uzbek-callcenter-medium \
  -e LOCAL_TRANSCRIPTION_CATALOG_PATH=/catalog/catalog.json \
  -e LOCAL_TRANSCRIPTION_RETRY_UNCERTAIN=true \
  -e HF_HUB_OFFLINE=1 -e HF_HUB_DISABLE_TELEMETRY=1 -e PYANNOTE_METRICS_ENABLED=0 \
  -e OMP_NUM_THREADS=2 -e OPENBLAS_NUM_THREADS=1 \
  -v /opt/texnikach-transcription-20260918/venv:/stage/venv:ro \
  -v /opt/texnikach-transcription-20260918/models/diarization:/diarization:ro \
  -v /opt/texnikach-transcription-models:/models:ro \
  -v /opt/texnikach-asr-quality-20260919:/quality \
  -v /opt/texnikach-asr-quality-20260919/catalog:/catalog:ro \
  -v nylgfmvjodgie9dga7ngprgl-botmoizvonki-data:/calls:ro \
  texnikach-transcription-worker:20260919-uzbek-callcenter \
  /stage/venv/bin/python /quality/project/scripts/compare_call_asr.py \
  --variant "$variant" --navai-path /quality/models/navai-uzbek-medium \
  --output /quality/results "$@"
