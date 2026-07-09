# Kurukin A-roll/B-roll Prepare B-roll UI

## Objetivo

Render Console agrega "Preparar B-roll" como paso previo a validar y encolar.
La accion convierte una policy, query y candidatos locales en una lista local
para `b_roll.assets`.

## Comportamiento

- guarda el resultado en `st.session_state["aroll_broll_prepared_assets"]`.
- muestra assets preparados, contador, `source_provider`, policy label y query.
- si el resultado es OK, alimenta el campo B-roll local del enqueue UI.
- no crea pending automaticamente.
- no ejecuta render.

## Policies

`local_only`:

- modo seguro inicial.
- usa solo candidatos locales bajo `storage/local_videos`,
  `storage/local_assets` o `storage/local_images`.
- falla si no hay suficientes candidatos.

`open_sources`:

- usa candidatos locales primero.
- en UI inicial no pasa downloader real con flags apagados.
- si faltan candidatos y el flag esta apagado, muestra:
  `No hay suficientes assets locales. Pexels no está activo en esta consola.`
- Pexels queda disponible solo con integracion controlada y
  `KURUKIN_ENABLE_PEXELS_SOURCE=1`.
- El flag registra Pexels como adapter opcional; no reemplaza candidatos
  locales ni otros adapters permitidos por la policy.
- cuando se habilite en una prueba controlada, debe usarse prepare-only.

`exclusive_brand_assets`:

- requiere manifest local o bundle UID resoluble a manifest local.
- no llama Asset Hub API.
- no usa Pexels ni downloader.

## Guardrails

- no runner.
- no `scripts/nightly_runner.py`.
- no `scripts/local_job_wrapper.py`.
- no ffmpeg real.
- no Pexels real.
- no Asset Hub API.
- no `/api/v1/videos`.
- no pending.
- no task.
- enqueue/render siguen en pasos separados y protegidos por flags.

## Copy visible

La consola muestra:

`Pexels source: disponible solo con integración controlada/flag; no se usa por defecto.`

No se muestra API key ni se prueba Pexels real desde la UI con el flag apagado.
