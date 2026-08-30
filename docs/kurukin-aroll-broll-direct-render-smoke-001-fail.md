# A-roll / B-roll direct render smoke 001 failure

Fecha: 2026-07-08

Branch: `feature/aroll-broll-direct-render-real-smoke-001`

Base: `a62239b merge: add a-roll b-roll direct render dry-run smoke`

Task id: `aroll-broll-direct-smoke-001`

## Resultado

- `ok=false`
- Output esperado: `storage/tasks/aroll-broll-direct-smoke-001/final-1.mp4`
- Output creado: no
- Causa probable: el directorio padre del output no existia antes de invocar
  ffmpeg.
- El helper anterior no imprimia `returncode`, `stdout` ni `stderr`, por lo que
  el stderr capturado no quedo visible en consola.

## Fixtures creados

- `storage/local_videos/_aroll_broll_smoke_001/aroll_presenter_fixture_6s.mp4`
- `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`

## Guardrails verificados

- `storage/nightly_jobs/pending` vacio.
- No se ejecuto runner.
- No se llamo API ni `/api/v1/videos`.
- No se ejecuto `scripts/local_job_wrapper.py`.
- No se repitio `--execute` real despues del fallo.
- No se stageo `storage`.

## Fix aplicado

- `run_aroll_broll_render(..., dry_run=False)` crea el parent dir del output
  planificado justo antes de invocar ffmpeg o el runner inyectado.
- `dry_run=True` no crea el task dir.
- El smoke JSON expone `returncode`, `stdout` y `stderr` para diagnostico.
