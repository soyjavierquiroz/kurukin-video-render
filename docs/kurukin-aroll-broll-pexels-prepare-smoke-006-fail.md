# A-roll / B-roll Pexels prepare-only smoke 006 FAIL

Fecha: 2026-07-09

Branch: feature/aroll-broll-pexels-prepare-smoke-006-pass

Base branch: custom/mvp

Base commit at branch cut: fcc8461156b587942aace9a15dce32020ef16a6c

Task id: aroll-broll-pexels-prepare-smoke-006

## Objetivo

Validar prepare-only real con Pexels:

- usar Pexels source adapter real
- materializar 3 B-roll MP4 locales
- no render
- no runner
- no pending
- no task
- no ffmpeg
- no ffprobe

## Query

`modern coffee shop b roll`

## Output dir esperado

`storage/local_videos/_aroll_broll_pexels_prepare_smoke_006/`

## Resultado

FAIL despues de contactar Pexels real una sola vez.

La verificacion booleana previa dentro del contenedor webui devolvio:

```text
{
  "pexels_key_available": true,
  "pixabay_key_available": true,
  "coverr_key_available": true
}
```

La unica ejecucion real autorizada de prepare-only fallo en la etapa de busqueda
de videos Pexels con:

```text
urllib.error.HTTPError: HTTP Error 403: Forbidden
```

No se repitio la llamada real despues de ese fallo.

## Ejecucion real

- Pexels real fue contactado exactamente una vez.
- Pixabay y Coverr no fueron usados.
- El fallo ocurrio antes de materializar assets locales.
- No hubo resultado JSON exitoso (`ok=true` no se obtuvo).
- No hubo descarga parcial util.
- El output dir esperado no fue creado.
- No existen assets parciales bajo
  `storage/local_videos/_aroll_broll_pexels_prepare_smoke_006/`.

## Guardrails

- API keys no impresas ni commiteadas.
- El log de ejecucion no contiene `Authorization`, `Bearer`, `api_key`,
  `ffmpeg`, `ffprobe`, `scripts/nightly_runner.py`, `local_job_wrapper`,
  `POST http`, `POST https` ni `/api/v1/videos`.
- No pending.
- No task.
- No runner.
- No render.
- No MPT API.
- No Asset Hub API.
- No DB, rclone ni credenciales.
- No `config.toml`.
- No `resource/fonts`.
- `.env` sigue ignored y no stageado.
- `storage/` sigue ignored y no stageado.

## Runtime read-only

- HTTP Render Console: 200
- Flags finales:
  - `KURUKIN_ENABLE_UI_RUNNER=<unset>`
  - `KURUKIN_ENABLE_AROLL_BROLL_QUEUE=<unset>`
  - `KURUKIN_ENABLE_AROLL_BROLL_RENDERER=<unset>`
  - `KURUKIN_ENABLE_AROLL_BROLL_DIRECT_RENDER=<unset>`
  - `KURUKIN_ENABLE_PEXELS_SOURCE=<unset>`
- pending vacio
- no task smoke-006
- sin output dir smoke-006
- Validacion HTML read-only limitada:
  `curl` devuelve el shell de Streamlit; no hubo un AppTest interactivo formal
  en esta corrida.

## Checks

- `py_compile`: OK
- `unittest`: OK, 289 tests, 6 skipped
- `git diff --check`: OK
- `docker compose config`: OK

## Proximos pasos

1. Revisar por que Pexels responde `403 Forbidden` aun con presencia booleana
   de la key en el contenedor.
2. Confirmar fuera de git si la key tiene permisos vigentes para
   `https://api.pexels.com/v1/videos/search`.
3. Solicitar una nueva autorizacion explicita antes de cualquier nuevo contacto
   real con Pexels.
