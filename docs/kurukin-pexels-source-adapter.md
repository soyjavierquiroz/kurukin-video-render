# Kurukin Pexels Source Adapter

## Estado arquitectonico

Fallback experimental/no primario.

La ruta preferida para stock media es MoneyPrinterTurbo nativo:

- `app.services.material.search_videos_pexels()`
- `app.services.material.search_videos_pixabay()`
- `app.services.material.search_videos_coverr()`
- `app.services.material.download_videos()`

Kurukin debe compilar intents a specs MPT con
`app/custom/mpt_engine_bridge.py` y dejar sourcing/render al motor MPT cuando el
render real este autorizado.

No crear adapters propios para Pixabay/Coverr hasta cerrar esa ruta.

## Objetivo

Pexels queda preparado como fuente opcional para `open_sources` dentro del flujo
"Preparar B-roll". El adapter solo busca, selecciona y materializa archivos
locales cuando un caller controlado le inyecta credenciales y ejecución.

Esta rama no ejecuta Pexels real, no descarga assets reales y no renderiza.

## Contrato

- Pexels no es fuente unica ni default.
- Pexels custom no es ruta primaria si MPT puede usar su proveedor nativo.
- Solo aplica a `asset_policy.mode=open_sources`.
- El materializer sigue siendo generico: combina candidatos locales y adapters
  inyectados permitidos, deduplica y devuelve paths locales.
- Los adapters se registran por `source_provider` y se recorren segun
  `asset_policy.allowed_sources`; Pexels es solo una entrada opcional.
- La secuencia es local primero y adapters permitidos despues.
- No aplica a `exclusive_brand_assets`.
- `local_only` nunca usa Pexels.
- El adapter entrega paths locales para `b_roll.assets`.
- El renderer no llama Pexels ni proveedores externos.
- Asset Hub API no participa en este adapter.

## Seguridad

- La API key se lee solo mediante `get_pexels_api_key()` o al crear un
  downloader explicito.
- La API key no se imprime, no se guarda y no se commitea.
- El header de Pexels usa `Authorization: <api_key>` directo, sin `Bearer`.
- El endpoint preparado es `https://api.pexels.com/v1/videos/search`.
- Las descargas solo se escriben bajo `storage/local_videos` o
  `storage/local_assets`.
- Los tests usan `opener` fake; no hay network real.

## Metadata de atribucion

Cuando Pexels entrega datos disponibles, cada asset materializado conserva:

```json
{
  "source_provider": "pexels",
  "pexels_video_id": "101",
  "photographer": "Nombre",
  "photographer_url": "https://www.pexels.com/@autor/",
  "pexels_url": "https://www.pexels.com/video/example/",
  "width": 720,
  "height": 1280,
  "path": "storage/local_videos/pexels-101-1.mp4"
}
```

## Render Console

La consola muestra:

`Pexels source: disponible solo con integración controlada/flag; no se usa por defecto.`

El flag de seguridad es:

`KURUKIN_ENABLE_PEXELS_SOURCE=1`

Con el flag apagado, si `open_sources` no tiene suficientes candidatos locales,
la UI muestra:

`No hay suficientes assets locales. Pexels no está activo en esta consola.`

## Proxima prueba real

La primera prueba real debe ser prepare-only:

- habilitar el flag solo en una ejecucion controlada.
- usar query y desired_count chicos.
- verificar metadata y paths locales.
- no ejecutar render.
- no ejecutar runner.
- no llamar `/api/v1/videos`.
