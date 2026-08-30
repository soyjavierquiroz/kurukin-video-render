# Kurukin batch manual process smoke 001 PASS

- commit probado: `e567182 feat: add batch manual processing for intent queue`
- task prefix: `kurukin-batch-manual-process-smoke`
- audios usados:
  - `storage/local_audios/mpt_native_console_smoke_002.mp3`
  - `storage/local_audios/mpt_native_console_smoke_002.mp3`
- topic: `vertical reel batch manual process smoke`

## Queue items

- task_id: `kurukin-batch-manual-process-smoke-001`
  - queue item path: `storage/nightly_jobs/pending/20260712-213432-kurukin-batch-manual-process-smoke-001.json`
  - visual resuelto: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
  - output: `storage/tasks/kurukin-batch-manual-process-smoke-001/final-1.mp4`
  - status final: `DONE`
  - ffprobe: duration `4.000000`, size `205727` bytes, video `h264` `1080x1920`, audio `aac`
- task_id: `kurukin-batch-manual-process-smoke-002`
  - queue item path: `storage/nightly_jobs/pending/20260712-213432-kurukin-batch-manual-process-smoke-002.json`
  - visual resuelto: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
  - output: `storage/tasks/kurukin-batch-manual-process-smoke-002/final-1.mp4`
  - status final: `DONE`
  - ffprobe: duration `4.000000`, size `206241` bytes, video `h264` `1080x1920`, audio `aac`

## Flujo validado

batch audio inputs -> enqueue -> batch manual process -> MPT nativo -> `final-1.mp4` por item.

## Confirmaciones

- sin runner
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
