# A-roll / B-roll multiple B-roll E2E smoke 005 PASS

Fecha: 2026-07-08

Branch: feature/aroll-broll-multiple-broll-e2e-smoke-005

Base: 33d11e5bb342f7f410dac4af24595a903e8ae41c

Task id: aroll-broll-multi-smoke-005

## Fixtures

A-roll:
- storage/local_videos/_aroll_broll_smoke_001/aroll_presenter_fixture_6s.mp4

B-roll Pexels fixtures:
- storage/local_videos/_aroll_broll_smoke_005/broll_pexels_01.mp4
- storage/local_videos/_aroll_broll_smoke_005/broll_pexels_02.mp4
- storage/local_videos/_aroll_broll_smoke_005/broll_pexels_03.mp4

Notas Pexels:
- Se usaron credenciales existentes del entorno/proyecto.
- No se imprimio ni commiteo la API key.
- Los binarios quedan en storage ignored.

## UI path

Pending creado con:
- app.custom.kurukin_render_console.enqueue_aroll_broll_from_console
- b_roll.assets con 3 assets locales
- layout alternating_fullscreen
- subtitles none
- quality draft_720p

## Queue

- Pending file: storage/nightly_jobs/pending/20260709-001207-aroll-broll-multi-smoke-005.json
- Pending consumido por runner.
- Job movido a completed.
- Completed dir: storage/nightly_jobs/completed/20260709-001207-aroll-broll-multi-smoke-005-20260709T001256Z-77-1783555976343351473
- No quedo pending.
- No quedo failed.

Artifacts:
- submit-response.json
- final-task.json
- render-result.json
- no error.json

## Runner

Comando ejecutado una sola vez, dentro del contenedor webui con el repo montado:

```bash
docker exec -e KURUKIN_ENABLE_AROLL_BROLL_RENDERER=1 moneyprinterturbo-webui bash -lc 'cd /MoneyPrinterTurbo && python3 scripts/nightly_runner.py --max-jobs 1 --ignore-window --queue-dir /MoneyPrinterTurbo/storage/nightly_jobs --api-base-url http://127.0.0.1:18080/api/v1'
```

Resultado:
- jobs_started=1
- sin /api/v1/videos
- sin POST http/https
- sin UI runner
- sin local_job_wrapper

## Multi B-roll

- b_roll_asset_count: 3
- rotacion ciclica esperada en alternating_fullscreen
- audio final: A-roll original
- B-roll audio: muted/no mapeado
- duracion final: clamped a duracion A-roll

## Output

- storage/tasks/aroll-broll-multi-smoke-005/final-1.mp4
- tamano: 3,630,084 bytes (3.5M)
- duracion: 6.000000s
- resolucion: 720x1280
- video stream: h264 presente, avg_frame_rate 30/1
- audio stream: aac presente

## Render Console

- HTTP: 200, 1522 bytes.
- AppTest exception_count=0.
- Presentador + B-roll visible.
- Audio: A-roll original visible.
- B-roll muted visible.
- B-roll assets: 3 visible.
- aroll-broll-multi-smoke-005 detectado/listado.
- Preview/download OK.
- Download bytes: 3,630,084.
- Sin pending nuevo.
- Sin task nuevo adicional.
- Flags unset post-run.
- Sin HTML crudo.

## Checks

- py_compile OK.
- unittest custom OK: 210 tests, 5 skipped.
- git diff --check OK.
- docker compose -f docker-compose.local.yml config --quiet OK.

## Guardrails

- Runner real ejecutado una sola vez.
- ffmpeg real ejecutado indirectamente una sola vez via runner handler.
- No UI runner.
- No KURUKIN_ENABLE_UI_RUNNER.
- No scripts/local_job_wrapper.py.
- No API ni /api/v1/videos.
- No Asset Hub API.
- No DB/rclone/credenciales.
- No Pexels key expuesta ni commiteada.
- No storage stageado.
- smoke-001/002/003/004 preservados.
- MP4 previos del MVP preservados.
- Output y B-roll fixtures quedan en storage y no se incluyen en git.
- No main/config.toml/resource/fonts/Asset Hub code/API tocados.
- No merge, no push, no tag.
