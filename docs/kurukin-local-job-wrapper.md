# Kurukin Local Job Wrapper

`scripts/local_job_wrapper.py` convierte un JSON con `selectedAssets` locales y
configuracion de video en un job compatible con `scripts/nightly_runner.py`.
No renderiza video ni llama a la API de MoneyPrinterTurbo; solo valida assets y,
cuando se solicita, escribe un JSON en `storage/nightly_jobs/pending/`.

## Uso

Validar sin escribir en la cola:

```bash
python3 scripts/local_job_wrapper.py examples/local-job.example.json --validate-only
```

Imprimir el payload que quedaria encolado:

```bash
python3 scripts/local_job_wrapper.py examples/local-job.example.json --print-payload
```

Encolar en la ruta default:

```bash
python3 scripts/local_job_wrapper.py examples/local-job.example.json --enqueue
```

Encolar en otra cola o validar contra otra carpeta de assets:

```bash
python3 scripts/local_job_wrapper.py examples/local-job.example.json --enqueue --queue-dir storage/nightly_jobs
python3 scripts/local_job_wrapper.py examples/local-job.example.json --enqueue --local-videos-dir storage/local_videos
```

Para pruebas con archivos dummy, omita `ffprobe`:

```bash
python3 scripts/local_job_wrapper.py examples/local-job.example.json --validate-only --skip-media-probe
```

## Entrada esperada

El JSON debe ser un objeto con:

- `job_id`: string requerido.
- `description`: string opcional.
- `selectedAssets`: lista no vacia de objetos con `file`; `label` y `order` son
  metadatos opcionales.
- `video`: objeto con la configuracion MoneyPrinterTurbo. `video_subject`,
  `video_script` y `video_aspect` son requeridos.

`selectedAssets` se ordena por `order` cuando existe. Si ningun asset tiene
`order`, se mantiene el orden original.

## Validaciones de assets

Cada `file` debe ser solo nombre de archivo, sin rutas absolutas, `../`,
separadores `/`, separadores `\`, nombres vacios ni duplicados. Las extensiones
permitidas son:

```text
mp4, mov, avi, flv, mkv, jpg, jpeg, png
```

El archivo debe existir dentro de `--local-videos-dir`. El wrapper resuelve
symlinks con `Path.resolve()` y rechaza cualquier archivo real que termine fuera
de esa carpeta.

Por defecto, cada asset se valida con `ffprobe` y el primer stream visual debe
tener `width` y `height` mayores o iguales a `--min-width` y `--min-height`
(ambos default `480`). Use `--skip-media-probe` solo para tests o validaciones
estructurales.

## Payload generado

El job pendiente conserva metadatos para el runner:

```json
{
  "job_id": "relaciones-local-demo-001",
  "description": "Demo usando assets locales seleccionados",
  "runner": {
    "source": "local_job_wrapper",
    "selectedAssets": [
      { "file": "clip-01.mp4", "label": "intro", "order": 1 }
    ]
  },
  "video_subject": "La importancia de escoger bien a tu pareja",
  "video_script": "Escoger bien a tu pareja puede cambiar por completo el rumbo de tu vida.",
  "video_aspect": "9:16",
  "video_source": "local",
  "video_materials": [
    { "provider": "local", "url": "clip-01.mp4", "duration": 0 }
  ]
}
```

`selectedAssets` queda dentro de `runner`, no como key raiz. Esto importa porque
`nightly_runner.py` elimina `job_id`, `description`, `notes` y `runner` antes de
enviar el payload final a MoneyPrinterTurbo.

La escritura en `pending/` es atomica: primero se crea un archivo temporal en la
misma carpeta y luego se reemplaza por el JSON final.
