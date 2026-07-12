# Kurukin topic with audio v1 smoke 001 PASS

- task_id: `kurukin-topic-with-audio-smoke-001`
- mode: `topic_to_video`
- commit probado: `578c8f4 feat: support topic intents with provided audio`
- input: `topic` + `audio_path` local; sin `video_path`; sin `visual_path` manual
- topic: `5 errores al comprar una casa usada`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- status compilado: `READY_TO_SUBMIT`
- reason: none
- visual local resuelto: `storage/local_videos/Espacio de trabajo místico con cuaderno abierto, tablet con símbolos lunares, velas parpadeando suavemente, carta sellada con lavanda, audífonos y café, movimiento sutil de luz de vela.mp4`
- visual_autofill_source: `local_picker_v1`
- queue item: `storage/nightly_jobs/pending/20260712-224614-kurukin-topic-with-audio-smoke-001.json`
- status en cola: `QUEUED`

## Script generado resumido

Hook local sobre los riesgos de comprar una casa usada, seguido por dos puntos cortos: revisar datos visibles y comparar opciones antes de decidir.

## Topic plan

- status: `NEEDS_AUDIO`
- reason: `needs_audio_or_tts`
- preset: `educational`
- language: `es`
- duration_seconds: `4`
- scenes: `3`
- visual_keywords: `5 errores al comprar una casa usada`, `checklist`, `detalle`, `explicacion`, `errores`, `comprar`, `casa`, `usada`

## Campos confirmados

- `script`
- `topic_plan`
- `resolved_visual_path`
- `visual_autofill_source`: `local_picker_v1`
- `audio_path`
- `compiled_mpt_spec`
- `compiled_mpt_spec.mpt_params.custom_audio_file`: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- `source`: `job_intent_v1`
- `runner.execution_mode`: `manual_queue_only`
- `guardrails.external_providers_allowed`: `false`
- `guardrails.ai_generation_allowed`: `false`
- `guardrails.asset_hub_api_allowed`: `false`
- `guardrails.real_render_started`: `false`

## Flujo validado

`topic_to_video` + `audio_path` -> topic planner local -> `local_picker_v1` -> `compile_job_intent_to_mpt_spec` -> `READY_TO_SUBMIT` -> `enqueue_moneyprinter_payload` -> pending JSON/item creado con `QUEUED`.

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
