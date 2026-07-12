# Kurukin manual queue process smoke 001 PASS

- task_id: `kurukin-manual-queue-process-smoke-001`
- commit probado: `0cc6c56 feat: add manual processing for intent queue`
- queue item path: `storage/nightly_jobs/pending/20260712-202459-kurukin-manual-queue-process-smoke-001.json`
- mode: `audio_to_video`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- topic: `vertical reel manual queue smoke`
- visual local resuelto: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
- visual_autofill_source: `local_picker_v1`
- output path: `storage/tasks/kurukin-manual-queue-process-smoke-001/final-1.mp4`
- status final cola: `DONE`

## ffprobe

- duration: `4.000000`
- size: `206289` bytes
- video: `h264`, `1080x1920`
- audio: `aac`

## Flujo validado

intent -> enqueue -> manual process -> `mpt_engine_bridge` -> `VideoParams` -> `task.start` nativo -> `final-1.mp4`

## Confirmaciones

- sin runner
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
