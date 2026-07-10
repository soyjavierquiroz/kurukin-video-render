# MPT native local smoke 001 PASS

Fecha: 2026-07-10

Task id: mpt-native-local-smoke-001

## Objetivo

Validar que Kurukin puede generar un spec con `mpt_engine_bridge`, validarlo contra `VideoParams` y ejecutarlo con el motor nativo MoneyPrinterTurbo usando local-only.

## Resultado

- PASS
- output: storage/tasks/mpt-native-local-smoke-001/final-1.mp4
- video fixture: storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4
- MPT local material url: _aroll_broll_smoke_001/broll_visual_fixture_2s.mp4
- audio fixture source: storage/local_audios/mpt_native_local_smoke_001.wav
- audio fixture used by MPT: storage/local_audios/mpt_native_local_smoke_001.mp3
- duration: 4.000000
- size: 206218
- streams: video h264 1080x1920 30/1 + audio aac

## Guardrails

- sin Pexels/Pixabay/Coverr
- sin descargas externas
- sin runner
- sin pending
- sin /api/v1/videos
- sin Asset Hub API
- sin secretos
- .env ignored/no stageado
- storage ignored/no stageado
- task real creado por motor nativo MPT
- render real ejecutado por motor nativo MPT

## Checks

- HTTP Render Console: 200
- ffprobe OK
- pending vacio
- contenedor usado: moneyprinterturbo-api
- submit: app.services.task.start
