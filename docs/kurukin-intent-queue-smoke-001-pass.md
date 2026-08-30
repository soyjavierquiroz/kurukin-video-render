# Kurukin intent queue smoke 001 PASS

- task_id: `kurukin-intent-queue-smoke-001`
- mode: `audio_to_video`
- commit probado: `b96035f feat: add intent queue support`
- input: `audio_path` local + `topic`; sin `video_path`; sin `visual_path` manual
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- topic: `vertical reel queue smoke`
- visual local resuelto: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
- visual_autofill_source: `local_picker_v1`
- queue item: `storage/nightly_jobs/pending/20260712-200732-kurukin-intent-queue-smoke-001.json`
- status en cola: `QUEUED`

## Campos guardados

- `task_id`: `kurukin-intent-queue-smoke-001`
- `status`: `QUEUED`
- `source`: `job_intent_v1`
- `mode`: `audio_to_video`
- `original_intent`
- `normalized_intent`
- `compiled_mpt_spec`
- `resolved_visual_path`
- `visual_autofill_source`
- `runner.execution_mode`: `manual_queue_only`
- `guardrails.external_providers_allowed`: `false`
- `guardrails.ai_generation_allowed`: `false`
- `guardrails.asset_hub_api_allowed`: `false`
- `guardrails.real_render_started`: `false`

## Flujo validado

Render Console/helper -> `kurukin_job_intent` -> `local_picker_v1` -> `compile_job_intent_to_mpt_spec` -> `enqueue_moneyprinter_payload` -> pending JSON/item creado.

## Confirmaciones

- sin render
- sin submit MPT nativo
- sin runner
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin descargas
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
