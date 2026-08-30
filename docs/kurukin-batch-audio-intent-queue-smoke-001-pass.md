# Kurukin batch audio intent queue smoke 001 PASS

- commit probado: `104a6d5 feat: add batch audio intent queue`
- task prefix: `kurukin-batch-audio-smoke`
- audios usados:
  - `storage/local_audios/mpt_native_console_smoke_002.mp3`
  - `storage/local_audios/mpt_native_console_smoke_002.mp3`
- topic: `vertical reel batch smoke`
- items creados: `2`

## Queue items

- task_id: `kurukin-batch-audio-smoke-001`
  - queue item path: `storage/nightly_jobs/pending/20260712-212558-kurukin-batch-audio-smoke-001.json`
  - status: `QUEUED`
  - visual resuelto: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
  - visual_autofill_source: `local_picker_v1`
- task_id: `kurukin-batch-audio-smoke-002`
  - queue item path: `storage/nightly_jobs/pending/20260712-212558-kurukin-batch-audio-smoke-002.json`
  - status: `QUEUED`
  - visual resuelto: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
  - visual_autofill_source: `local_picker_v1`

## Flujo validado

batch audio inputs -> `audio_to_video` intents -> `local_picker_v1` -> `compile_job_intent_to_mpt_spec` -> `enqueue_moneyprinter_payload` -> queue items `QUEUED`.

## Confirmaciones

- sin render
- sin submit MPT nativo
- sin runner
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
