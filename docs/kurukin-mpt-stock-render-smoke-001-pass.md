# Kurukin MPT stock render smoke 001 PASS

- task_id: `kurukin-mpt-stock-visual-intent-smoke-001`
- queue item path: `storage/nightly_jobs/pending/20260713-035423-kurukin-mpt-stock-visual-intent-smoke-001.json`
- mode: `topic_to_video`
- source: `job_intent_v1`
- topic: `5 errores al comprar una casa usada`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- stock_source: `pexels`
- stock_terms: `5 errores al comprar una casa usada`, `checklist`, `detalle`, `explicacion`, `errores`, `comprar`, `casa`, `usada`
- output path: `storage/tasks/kurukin-mpt-stock-visual-intent-smoke-001/final-1.mp4`
- status final: `DONE`

## ffprobe

- duration: `4.000000`
- video codec: `h264`
- resolution: `1080x1920`
- audio codec: `aac`
- size: `2800709` bytes

## Queue item final

- `status`: `DONE`
- `output_path`: `storage/tasks/kurukin-mpt-stock-visual-intent-smoke-001/final-1.mp4`
- `source`: `job_intent_v1`
- `mode`: `topic_to_video`
- `runner.execution_mode`: `manual_queue_only`
- `normalized_intent.visual_source_mode`: `mpt_stock`
- `normalized_intent.stock_source`: `pexels`
- `normalized_intent.stock_terms`: presentes
- `compiled_mpt_spec.mpt_params.video_source`: `pexels`
- `compiled_mpt_spec.mpt_params.video_terms`: presentes
- `manual_process_result.validated_model`: `VideoParams`
- `manual_process_result.submit_target`: `app.services.task.start`
- `manual_process_result.stock_source`: `pexels`
- `guardrails.real_render_started`: `true`

## Flujo validado

`topic_to_video` + `audio_path` -> relevance guard -> MPT stock visual spec -> manual process -> `mpt_engine_bridge`/`VideoParams` -> `task.start` nativo MPT -> MPT native stock sourcing `pexels` -> `final-1.mp4`.

## Confirmaciones

- proveedor usado: `pexels`
- sin runner
- sin nightly execution
- sin `/api/v1/videos`
- sin OpenAI real
- sin TTS real
- sin Asset Hub API
- sin DB/rclone
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
- sin downloader custom
