# MPT native UI smoke 003 PASS

Fecha: 2026-07-11

Task id: mpt-native-ui-smoke-003

## Objetivo

Validar la UI minima "Motor MPT nativo" usando el helper exacto de Render Console para enviar un job local-only al motor nativo MoneyPrinterTurbo.

## Resultado

- PASS
- output: storage/tasks/mpt-native-ui-smoke-003/final-1.mp4
- video fixture: storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4
- audio fixture: storage/local_audios/mpt_native_console_smoke_002.mp3
- flujo validado: Render Console/helper -> mpt_engine_bridge -> VideoParams -> task.start nativo -> final-1.mp4
- ffprobe: duration 4.000000s, size 206361, video h264 1080x1920, audio aac
- container: moneyprinterturbo-api

## Guardrails

- KURUKIN_ENABLE_MPT_ENGINE_SUBMIT=1 solo durante ejecucion
- sin proveedores externos
- sin Pexels/Pixabay/Coverr
- sin runner
- sin nightly queue
- sin /api/v1/videos
- sin Asset Hub API
- sin cambios a config.toml
- sin cambios a resource/fonts
- storage ignored/no stageado
- task real creado por motor nativo MPT
- render real ejecutado por motor nativo MPT via helper protegido

## Checks

- python3 -m unittest tests.custom.test_kurukin_render_console: OK, 98 tests, 7 skipped
- python3 -m unittest tests.custom.test_mpt_engine_bridge tests.custom.test_mpt_engine_submitter: OK, 27 tests
- git diff --check: OK
- docker compose -f docker-compose.local.yml config --quiet: OK
- ffprobe OK
- pending vacio
