# MPT native console smoke 002 PASS

Fecha: 2026-07-09

Task id: mpt-native-console-smoke-002

## Objetivo

Validar la nueva accion/helper protegida de Render Console para enviar un job local-only al motor nativo MoneyPrinterTurbo usando `mpt_engine_bridge`, `VideoParams` y `task.start`.

## Resultado

- PASS
- output: storage/tasks/mpt-native-console-smoke-002/final-1.mp4
- video fixture: storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4
- audio fixture: storage/local_audios/mpt_native_console_smoke_002.mp3
- duration: 4.000000
- size: 206361
- streams: video + audio
- container: moneyprinterturbo-api

## Guardrails

- KURUKIN_ENABLE_MPT_ENGINE_SUBMIT=1 solo durante ejecucion
- sin Pexels/Pixabay/Coverr
- sin descargas externas
- sin runner
- sin pending/nightly queue
- sin /api/v1/videos
- sin Asset Hub API
- sin secretos
- .env ignored/no stageado
- storage ignored/no stageado
- task real creado por motor nativo MPT
- render real ejecutado por motor nativo MPT via helper protegido

## Checks

- HTTP Render Console: 200
- ffprobe OK
- pending vacio
