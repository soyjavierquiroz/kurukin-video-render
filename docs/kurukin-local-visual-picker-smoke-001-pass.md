# Kurukin Local Visual Picker Smoke 001 PASS

- task_id: `kurukin-local-picker-smoke-001`
- mode: `audio_to_video`
- commit probado: `4764ba4 feat: add local visual picker for audio intents`
- input: `audio_path` only + `topic`, sin `video_path`, sin `visual_path`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- topic: `vertical reel smoke`
- visual local resuelto: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
- visual_autofill_source: `local_picker_v1`
- output path: `storage/tasks/kurukin-local-picker-smoke-001/final-1.mp4`

## ffprobe resumido

- duration: `4.000000`
- video codec: `h264`
- resolution: `1080x1920`
- audio codec: `aac`
- size: `206195` bytes

## Flujo validado

`audio_path` only + `topic` -> `kurukin_job_intent` -> `local_picker_v1` -> `resolved_visual_path` -> `mpt_engine_bridge` -> `VideoParams` -> `app.services.task.start` nativo MPT -> `final-1.mp4`

## Confirmaciones

- sin proveedores externos
- sin OpenAI/TTS real
- sin runner/nightly queue
- sin `/api/v1/videos`
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
- sin deploy
- sin push
