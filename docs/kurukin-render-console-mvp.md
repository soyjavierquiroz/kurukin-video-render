# Kurukin Render Console MVP

La Kurukin Render Console es una pagina interna de Streamlit dentro de la
WebUI existente de MoneyPrinterTurbo. Su objetivo es crear, validar y encolar
jobs Kurukin para que el `nightly_runner` los procese despues.

No hace render directamente. No llama a la API de Asset Hub, OpenAI, rclone ni
DB. La consola solo construye un Kurukin Job Spec, lo valida con
`app/custom/kurukin_job_adapter.py` y escribe el payload MoneyPrinterTurbo en
`storage/nightly_jobs/pending` usando `app/custom/kurukin_job_queue.py`.

## Flujo

```text
Kurukin Render Console UI
  -> app/custom/kurukin_render_console.py
  -> app/custom/kurukin_job_adapter.py
  -> app/custom/kurukin_job_queue.py
  -> storage/nightly_jobs/pending
  -> scripts/nightly_runner.py
```

## Tabs

- `Nuevo render`: formulario basico para crear specs Asset Hub manifest-first.
- `JSON avanzado`: editor JSON para pegar un Kurukin Job Spec completo.
- `Cola y storage`: vista read-only de cola nightly, tasks y resumen de storage.

## UX polish

La consola muestra labels operativos en espanol, un panel de estado y metricas
para que el operador vea claramente el modo, la cola destino y que la accion
principal solo encola. El spec y el payload completo quedan dentro de expanders
colapsados para evitar que el JSON ocupe la pantalla principal.

El tab `Nuevo render` usa defaults seguros para desarrollo local:

- `render_quality`: `draft_720p`.
- `subtitles.mode`: `none`.
- `image_motion.enabled`: `true`.
- `image_motion.preset`: `slow_zoom_in`.
- `asset_hub.bundle_uid`: bundle de prueba local.

## `material_count: 0` en modo Asset Hub

En modo Asset Hub manifest-first, `material_count: 0` en el payload es esperado.
La consola no expande assets dentro de `video_materials`; solo conserva:

- `asset_hub_renderer_manifest_path`
- `asset_hub_bundle_uid`
- `asset_hub_scene_mode`
- `asset_hub_strict`

El worker lee el renderer manifest al iniciar el render y ahi resuelve los
assets locales. Por eso la UI muestra un resumen amigable del manifest y una
nota explicita cuando el payload no trae materiales pero el manifest si contiene
assets.

## Flujo recomendado

1. Asset Hub crea y materializa un bundle.
2. Pegar `bundle_uid` o `renderer_manifest_path`.
3. Configurar audio, subtitulos, calidad, formato y motion.
4. Validar payload.
5. Encolar job.
6. El nightly runner procesa el job pendiente.

## Asset Hub Renderer Manifest

El MVP trabaja con manifests locales bajo:

```text
/data/job-assets/<bundle_uid>/manifests/renderer-manifest.json
```

La consola puede autogenerar ese path desde `bundle_uid`, pero no busca assets,
no descarga archivos y no inspecciona DBs de Asset Hub.

Para UX, la consola puede leer el renderer manifest local y mostrar solo un
resumen seguro: bundle, job id, escenas, cantidad de assets, duracion
aproximada, warnings, conteos de revision y primeros filenames. No imprime el
manifest completo.

## JSON avanzado

`JSON avanzado` permite pegar un Kurukin Job Spec completo para pruebas o casos
fuera del formulario basico. Validar JSON produce el mismo resumen de operador
que el formulario y mantiene el payload completo dentro de un expander. Encolar
desde este tab tambien escribe un archivo en `storage/nightly_jobs/pending`; no
ejecuta render.

## Cola y storage

`Cola y storage` es read-only. Muestra conteos de `pending`, `processing`,
`completed`, `failed`, tasks con `final-1.mp4` y storage total. Tambien lista
jobs y tasks con `st.dataframe` cuando hay entradas. La limpieza destructiva
queda para una fase posterior.

## Limitaciones MVP

- No hay cleanup destructivo.
- No hay preview visual.
- No hay selector manual avanzado de assets en formulario basico.
- No hay API directa hacia Asset Hub.
- No hay render directo desde la UI.
- No hay integracion con rclone ni credenciales.
