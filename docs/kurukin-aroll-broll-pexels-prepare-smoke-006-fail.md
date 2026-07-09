# A-roll / B-roll Pexels prepare-only smoke 006 FAIL

Fecha: 2026-07-09

Branch: feature/aroll-broll-pexels-prepare-smoke-006

Base: dafed02b387b4609d3cb7fbad4257375cb0b54b9

Task id: aroll-broll-pexels-prepare-smoke-006

## Objetivo

Validar prepare-only real con Pexels:

- usar Pexels source adapter real
- materializar 3 B-roll MP4 locales
- no render
- no runner
- no pending
- no ffmpeg

## Resultado

FAIL/BLOCKER antes de contactar Pexels.

La inspeccion segura de presencia mediante `get_pexels_api_key()` devolvio:

```text
pexels_key_available: false
```

La API key de Pexels no estaba disponible en el entorno del contenedor webui.
No se imprimio, busco, modifico ni guardo ninguna credencial.

## Ejecucion real

- Pexels real no fue contactado.
- La preparacion real no fue ejecutada.
- No hubo reintento ni alternativa por scraping.
- No hubo respuesta de Pexels.
- No hubo descarga parcial.
- No existen assets parciales.
- El output dir
  `storage/local_videos/_aroll_broll_pexels_prepare_smoke_006/` no fue creado.

## Guardrails

- API key no impresa ni commiteada.
- No pending.
- No task.
- No runner.
- No `scripts/nightly_runner.py`.
- No `scripts/local_job_wrapper.py`.
- No ffmpeg, ffprobe ni render.
- No MPT API.
- No `/api/v1/videos`.
- No Asset Hub API.
- No DB, rclone ni credenciales.
- No `config.toml`.
- No `resource/fonts`.
- `storage/` sigue ignored y no se stagea.

## Runtime read-only

- HTTP Render Console: 200
- AppTest `exception_count=0`
- "Preparar B-roll" visible
- copy/guardrail de Pexels visible
- enqueue deshabilitado con queue flag off
- runner deshabilitado con UI flag off
- flags unset al final, incluido `KURUKIN_ENABLE_PEXELS_SOURCE`
- pending vacio
- no task smoke-006
- sin HTML crudo

## Checks

- `py_compile`: OK
- `unittest`: OK, 285 tests, 6 skipped
- `git diff --check`: OK
- `docker compose config`: OK

## Proximos pasos

1. Configurar `PEXELS_API_KEY` mediante el mecanismo controlado del entorno,
   sin modificar `config.toml` ni commitear credenciales.
2. Confirmar solo su presencia booleana en el contenedor webui.
3. Solicitar una nueva autorizacion explicita antes de ejecutar Pexels real.
