# A-roll / B-roll direct render smoke 002 PASS

Fecha: 2026-07-08

Branch: feature/aroll-broll-direct-render-real-smoke-001

Base: a62239b merge: add a-roll b-roll direct render dry-run smoke

Fixes aplicados:
- 97fd05e fix: prepare a-roll b-roll direct render output directory
- 0cbf2a7 fix: clamp a-roll b-roll render duration

Task id: aroll-broll-direct-smoke-002

## Fixtures

- A-roll: storage/local_videos/_aroll_broll_smoke_001/aroll_presenter_fixture_6s.mp4
- B-roll: storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4

## Output

- storage/tasks/aroll-broll-direct-smoke-002/final-1.mp4
- tamaño: 1954406 bytes
- duración: 6.000000s
- resolución: 720x1280
- video stream: h264 presente
- audio stream: aac presente

## Resultado

- ok=true
- returncode=0
- dry_run=false
- renderer: direct smoke
- layout: alternating_fullscreen
- audio final: A-roll original
- B-roll audio: no mapeado/muted
- duración final: clamped a duración del A-roll

## Guardrails

- No runner.
- No scripts/nightly_runner.py.
- No scripts/local_job_wrapper.py.
- No pending.
- No API ni /api/v1/videos.
- No Asset Hub API.
- No DB/rclone/credenciales.
- No storage stageado.
- MP4 smoke-001 preservado.
- MP4 previos del MVP preservados.

## Notas

- El execute de smoke-002 se ejecutó una sola vez.
- El output binario queda en storage y no se incluye en git.
- Smoke-001 queda preservado como evidencia de duración incorrecta antes del fix 0cbf2a7.
