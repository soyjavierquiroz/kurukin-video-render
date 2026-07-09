# Kurukin A-roll/B-roll Asset Materializer

## Objetivo

El materializer convierte una `asset_policy` y una request de sourcing en paths
locales para `b_roll.assets`.

Esta fase es helper/schema/docs/tests. No ejecuta renderer, runner, ffmpeg,
Pexels real ni Asset Hub API.

## Request mínima

```json
{
  "asset_policy": {},
  "query": "city walking",
  "desired_count": 3,
  "output_dir": "storage/local_videos/_aroll_broll_materialized/<job_id>",
  "local_candidates": [],
  "manifest_path": null,
  "brand_asset_bundle_uid": null
}
```

## Resultado

```json
{
  "ok": true,
  "source_policy": {},
  "source_provider": "pexels",
  "b_roll_assets": ["storage/local_videos/example.mp4"],
  "b_roll_asset_count": 1,
  "metadata": {
    "query": "city walking",
    "materialized": true
  }
}
```

## Modes

`open_sources`:

- usa `local_candidates` primero.
- si faltan assets, puede completar con un registro generico
  `source_adapters`; conserva `downloader` como interfaz inyectada compatible.
- recorre solo adapters presentes en `asset_policy.allowed_sources`, en el
  orden declarado, y combina/deduplica sus paths locales.
- no llama Pexels directo si no hay `downloader`.
- puede aceptar `source_provider=pexels` y metadata de atribucion cuando el
  downloader controlado lo devuelve.
- todo adapter externo debe identificar `source_provider`; el materializer no
  asume Pexels ni otra fuente por defecto.
- respeta `allowed_sources`.

`local_only`:

- usa solo `local_candidates`.
- no usa downloader externo.
- falla si no hay suficientes candidatos locales.

`exclusive_brand_assets`:

- usa manifest local de marca.
- requiere `manifest_path` o `brand_asset_bundle_uid` resoluble a manifest local.
- usa `manifest_reader` inyectado o lectura local de JSON.
- no usa Pexels, fuentes abiertas ni downloader.

## Arquitectura

Materializer:

- decide/prepara fuentes.
- deduplica paths preservando orden.
- valida `desired_count` 1..8.
- valida paths bajo `storage/local_videos`, `storage/local_assets` o
  `storage/local_images`.
- entrega paths locales para `b_roll.assets`.

Render Console:

- expone "Preparar B-roll" como accion read-only/controlada.
- usa `local_only` como modo seguro inicial con candidatos locales.
- `open_sources` usa candidatos locales primero y solo podria completar con un
  downloader inyectado por integracion futura.
- no pasa downloader real en la UI inicial con flags apagados.
- Pexels source queda detras de `KURUKIN_ENABLE_PEXELS_SOURCE=1`.
- guarda assets preparados en session state para alimentar el campo
  `b_roll.assets`.
- no crea pending, no crea task, no ejecuta runner, no ejecuta ffmpeg.

Renderer:

- no decide fuentes.
- no llama Pexels ni proveedores externos.
- no llama Asset Hub API.
- consume `b_roll.assets` o manifest local ya materializado.

## Tests

Los tests usan downloader/manifest_reader fakes. No descargan Pexels real, no
llaman APIs, no crean pending, no crean task y no requieren storage real.

## Pexels adapter controlado

`app/custom/pexels_source.py` prepara un downloader compatible con el
materializer. Usa `https://api.pexels.com/v1/videos/search`, header
`Authorization` directo sin `Bearer`, selecciona MP4 verticales cuando puede y
guarda metadata de atribucion. Solo descarga cuando se llama explicitamente a la
funcion de descarga/downloader con un `opener` real o fake.

Ver: `docs/kurukin-pexels-source-adapter.md`.

## Render Console: Preparar B-roll

El flujo "Preparar B-roll" convierte policy + query + candidatos locales en una
lista local sugerida para `b_roll.assets`.

- muestra contador, `source_provider`, policy label, query y paths preparados.
- puede prellenar el campo B-roll local del enqueue UI.
- el enqueue/render sigue siendo un paso separado y protegido por flags.
- Pexels real queda para una integracion futura con downloader controlado.
- Asset Hub API no se llama; marca exclusiva usa manifest local o reader
  inyectado.
