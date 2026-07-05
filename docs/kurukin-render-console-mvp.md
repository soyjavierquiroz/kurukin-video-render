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

## Limitaciones MVP

- No hay cleanup destructivo.
- No hay preview visual.
- No hay selector manual avanzado de assets en formulario basico.
- No hay API directa hacia Asset Hub.
- No hay render directo desde la UI.
- No hay integracion con rclone ni credenciales.
