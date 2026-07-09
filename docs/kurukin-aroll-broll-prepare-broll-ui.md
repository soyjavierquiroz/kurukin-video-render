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
- en UI inicial no pasa downloader real.
- si faltan candidatos, muestra error claro.
- Pexels real queda para integracion futura con downloader controlado.

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
