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
- si faltan assets, puede completar con `downloader` inyectado.
- no llama Pexels directo si no hay `downloader`.
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

Renderer:

- no decide fuentes.
- no llama Pexels ni proveedores externos.
- no llama Asset Hub API.
- consume `b_roll.assets` o manifest local ya materializado.

## Tests

Los tests usan downloader/manifest_reader fakes. No descargan Pexels real, no
llaman APIs, no crean pending, no crean task y no requieren storage real.
