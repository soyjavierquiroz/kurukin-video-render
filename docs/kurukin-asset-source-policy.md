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

Renderer:

- no llama proveedores externos.
- no llama Asset Hub API.
- no usa rclone.
- consume `b_roll.assets`/manifest local.

## Relación con smoke-005

- Pexels se usó como `source_provider` temporal para fixtures reales.
- Los MP4 se materializaron localmente en storage.
- Renderer consumió paths locales genéricos.
- No se debe interpretar Pexels como fuente única.
