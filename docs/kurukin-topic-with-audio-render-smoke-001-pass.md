# Kurukin topic with audio render smoke 001 PASS

- task_id: `kurukin-topic-with-audio-smoke-001`
- queue item usado: `storage/nightly_jobs/pending/20260712-224614-kurukin-topic-with-audio-smoke-001.json`
- mode: `topic_to_video`
- source: `job_intent_v1`
- topic: `5 errores al comprar una casa usada`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- visual local resuelto: `storage/local_videos/Espacio de trabajo místico con cuaderno abierto, tablet con símbolos lunares, velas parpadeando suavemente, carta sellada con lavanda, audífonos y café, movimiento sutil de luz de vela.mp4`
- visual_autofill_source: `local_picker_v1`
- output path: `storage/tasks/kurukin-topic-with-audio-smoke-001/final-1.mp4`
- status final: `DONE`

## ffprobe

- duration: `5.000000`
- video codec: `h264`
- resolution: `1080x1920`
- audio codec: `aac`
- size: `1562329` bytes

## Queue item final

- `status`: `DONE`
- `output_path`: `storage/tasks/kurukin-topic-with-audio-smoke-001/final-1.mp4`
- `source`: `job_intent_v1`
- `mode`: `topic_to_video`
- `normalized_intent.script`: presente
- `normalized_intent.topic_plan`: presente
- `runner.execution_mode`: `manual_queue_only`
- `guardrails.external_providers_allowed`: `false`
- `guardrails.ai_generation_allowed`: `false`
- `guardrails.asset_hub_api_allowed`: `false`
- `guardrails.real_render_started`: `true`

## Flujo validado

`topic_to_video` + `audio_path` -> topic planner -> `local_picker_v1` -> queue -> manual process helper -> `mpt_engine_bridge` -> `VideoParams` -> `task.start` nativo MPT -> `final-1.mp4`.

## Nota

La relevancia visual todavía es básica. En este smoke se validó que el fallback local produce un render real pequeño; la mejora de selección visual por tema queda para una fase posterior.

## Confirmaciones

- sin runner
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
