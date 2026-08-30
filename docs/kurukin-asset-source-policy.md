# Kurukin Asset Source Policy

## Regla

Default:

- usar fuentes disponibles/autorizadas para encontrar recursos.

Marca exclusiva:

- si un job exige assets exclusivos de marca, usar solo Asset Hub/manifest local.

## Modes

`open_sources`:

- puede usar Asset Hub, Pexels, local library, uploaded.
- no obliga exclusividad.
- renderer sigue consumiendo assets locales.
- Pexels requiere adapter/downloader controlado; no se activa por defecto.

`exclusive_brand_assets`:

- usa solo Asset Hub/manifest local autorizado.
- requiere `brand_asset_bundle_uid`.
- no Pexels.
- no fuentes abiertas.

`local_only`:

- solo assets locales o subidos.
- no Pexels.
- no Asset Hub API.

## Arquitectura

Asset sourcing/materializer:

- decide fuentes.
- busca/descarga/materializa.
- entrega paths locales o manifest local.
- el helper minimo vive en `app/custom/asset_materializer.py` y usa
  downloader/manifest_reader inyectables en tests.

Renderer:

- no llama proveedores externos.
- no llama Asset Hub API.
- no usa rclone.
- consume `b_roll.assets`/manifest local.

## Materializer minimo

- `open_sources` usa candidatos locales primero y luego adapters inyectados
  permitidos, en el orden de `allowed_sources`.
- Los resultados de multiples adapters se combinan y deduplican; Pexels no
  desplaza a `local_library`, `uploaded`, Asset Hub/manifest ni fuentes futuras.
- Si el downloader se identifica como `pexels`, la policy debe permitir
  `pexels`.
- `local_only` usa solo candidatos locales y falla si no alcanzan.
- `exclusive_brand_assets` usa manifest local de marca y no usa downloader.
- El resultado final expone `b_roll_assets`, `b_roll_asset_count`,
  `source_provider` y metadata read-only para Cola/Resultados.
- No hay Pexels real ni Asset Hub API en tests.

## Pexels source adapter

El adapter Pexels vive en `app/custom/pexels_source.py` y conserva metadata de
atribucion (`photographer`, `photographer_url`, `pexels_url`, dimensiones y
path local) cuando esta disponible. La UI lo mantiene detras de
`KURUKIN_ENABLE_PEXELS_SOURCE=1`.

Ver: `docs/kurukin-pexels-source-adapter.md`.

## Relación con smoke-005

- Pexels se usó como `source_provider` temporal para fixtures reales.
- Los MP4 se materializaron localmente en storage.
- Renderer consumió paths locales genéricos.
- No se debe interpretar Pexels como fuente única.
