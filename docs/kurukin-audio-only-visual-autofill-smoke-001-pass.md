# Kurukin Audio-Only Visual Autofill Smoke 001 PASS

- task_id: `kurukin-audio-only-autofill-smoke-001`
- mode: `audio_to_video`
- commit probado: `e85ff52 feat: autofill local visual for audio intents`
- input: `audio_path` only, sin `video_path`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- visual local resuelto: `storage/local_videos/_aroll_broll_smoke_001/aroll_presenter_fixture_6s.mp4`
- output path: `storage/tasks/kurukin-audio-only-autofill-smoke-001/final-1.mp4`

## ffprobe resumido

- duration: `5.000000`
- video codec: `h264`
- resolution: `1080x1920`
- audio codec: `aac`
- size: `2509271` bytes

## Flujo validado

Render Console/helper -> `kurukin_job_intent` -> visual autofill local -> `compile_job_intent_to_mpt_spec` -> `mpt_engine_bridge` -> `VideoParams` -> `app.services.task.start` nativo -> `final-1.mp4`

## Confirmaciones

- sin proveedores externos
- sin OpenAI/TTS real
- sin runner
- sin `/api/v1/videos`
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
