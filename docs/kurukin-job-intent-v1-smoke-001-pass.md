# Kurukin Job Intent v1 Smoke 001 PASS

- task_id: `kurukin-intent-audio-video-smoke-001`
- mode: `audio_to_video`
- commit probado: `6dbc8bc feat: add kurukin job intent v1`
- output path: `storage/tasks/kurukin-intent-audio-video-smoke-001/final-1.mp4`

## ffprobe resumido

- duration: `5.000000`
- video codec: `h264`
- resolution: `1080x1920`
- audio codec: `aac`
- size: `2509295` bytes

## Flujo validado

Render Console/helper -> `kurukin_job_intent` -> `compile_job_intent_to_mpt_spec` -> `mpt_engine_bridge` -> `VideoParams` -> `app.services.task.start` nativo -> `final-1.mp4`

## Confirmaciones

- sin proveedores externos
- sin OpenAI/TTS real
- sin runner
- sin `/api/v1/videos`
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
