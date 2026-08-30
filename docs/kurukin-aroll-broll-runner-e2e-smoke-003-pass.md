# A-roll / B-roll runner E2E smoke 003 PASS

Fecha: 2026-07-08

Branch: feature/aroll-broll-runner-e2e-smoke-003

Base: 0860edce73eae86547ec18812376e0c1fd373bbf

Task id: aroll-broll-runner-smoke-003

## Fixtures

- A-roll: storage/local_videos/_aroll_broll_smoke_001/aroll_presenter_fixture_6s.mp4
- B-roll: storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4

## Runner

Comando ejecutado una sola vez:

KURUKIN_ENABLE_AROLL_BROLL_RENDERER=1 python3 scripts/nightly_runner.py --max-jobs 1 --ignore-window --queue-dir /opt/moneyprinterturbo/storage/nightly_jobs --api-base-url http://127.0.0.1:18080/api/v1

## Queue

- Pending creado con `render_mode="aroll_broll"`.
- Pending consumido por runner.
- Job movido a completed.
- No quedó pending.
- No quedó failed.
- No refs en API.

Pending file:

storage/nightly_jobs/pending/20260708-164846-aroll-broll-runner-smoke-003.json

Completed dir:

storage/nightly_jobs/completed/20260708-164846-aroll-broll-runner-smoke-003-20260708T164854Z-1560774-1783529334006377018

Artifacts:
- submit-response.json
- final-task.json
- render-result.json
- no error.json

## Output

- storage/tasks/aroll-broll-runner-smoke-003/final-1.mp4
- tamaño: 1954406 bytes (1.9M)
- duración: 6.000000s
- resolución: 720x1280
- video stream: h264 presente
- audio stream: aac presente

## Resultado

- state=1
- progress=100
- videos=["/tasks/aroll-broll-runner-smoke-003/final-1.mp4"]
- layout: alternating_fullscreen
- audio final: A-roll original
- B-roll audio: no mapeado/muted
- duración final: clamped a duración del A-roll

## Guardrails

- Runner real ejecutado una sola vez.
- No UI runner.
- No KURUKIN_ENABLE_UI_RUNNER.
- No scripts/local_job_wrapper.py.
- No API ni /api/v1/videos.
- No Asset Hub API.
- No DB/rclone/credenciales.
- No storage stageado.
- smoke-001 y smoke-002 preservados.
- MP4 previos del MVP preservados.
- Output binario queda en storage y no se incluye en git.
