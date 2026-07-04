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
python3 scripts/local_job_wrapper.py examples/local-job.example.json --enqueue --local-audios-dir storage/local_audios
python3 scripts/local_job_wrapper.py examples/local-job.example.json --enqueue --local-subtitles-dir storage/local_subtitles
python3 scripts/local_job_wrapper.py examples/local-job.example.json --enqueue --fonts-dir resource/fonts
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
- `audio`: objeto opcional. `audio.file` apunta a un archivo dentro de
  `storage/local_audios` o de `--local-audios-dir`.
- `subtitles`: objeto opcional. `subtitles.mode` acepta `whisper`,
  `custom_srt` o `none`.
- `video`: objeto con la configuracion MoneyPrinterTurbo. `video_subject`,
  `video_script` y `video_aspect` son requeridos.
- `subtitle_style_preset`: string opcional para aplicar un look de subtitulos.
- `subtitle_style_overrides`: objeto opcional para ajustar campos concretos del
  preset.

`selectedAssets` se ordena por `order` cuando existe. Si ningun asset tiene
`order`, se mantiene el orden original.

## Audio y subtitulos propios

El wrapper soporta audio propio como contrato formal sin cambiar el rol de
MoneyPrinterTurbo: sigue siendo el render worker. Por defecto busca:

- Videos en `storage/local_videos`.
- Audios en `storage/local_audios`.
- Subtitulos SRT en `storage/local_subtitles`.

Ejemplo con audio propio y Whisper real sin correccion contra `video_script`:

```json
{
  "audio": { "file": "audio-prueba.mp3" },
  "subtitles": {
    "mode": "whisper",
    "correction_enabled": false,
    "optimize": true
  },
  "video": {}
}
```

Ejemplo con SRT propio optimizado para vertical:

```json
{
  "audio": { "file": "audio-prueba.mp3" },
  "subtitles": {
    "mode": "custom_srt",
    "file": "audio-prueba.srt",
    "optimize": true
  },
  "video": {}
}
```

Ejemplo con SRT propio literal:

```json
{
  "audio": { "file": "audio-prueba.mp3" },
  "subtitles": {
    "mode": "custom_srt",
    "file": "audio-prueba.srt",
    "optimize": false
  },
  "video": {}
}
```

Ejemplo sin subtitulos:

```json
{
  "audio": { "file": "audio-prueba.mp3" },
  "subtitles": { "mode": "none" },
  "video": {}
}
```

Reglas:

- `audio.file` debe ser solo nombre de archivo. Extensiones permitidas:
  `.mp3`, `.wav`, `.m4a`, `.aac`, `.flac`, `.ogg`.
- `subtitles.file` debe ser solo nombre de archivo con extension `.srt`.
- No se aceptan rutas absolutas, `../`, `/` ni `\` en esos nombres.
- Si `audio.file` existe, el payload MoneyPrinterTurbo recibe
  `custom_audio_file: "storage/local_audios/<file>"`.
- `subtitles.mode = "whisper"` activa subtitulos, desactiva correccion por
  defecto y respeta `subtitles.correction_enabled` si se envia explicitamente.
- `subtitles.mode = "custom_srt"` activa subtitulos, envia
  `custom_subtitle_file: "storage/local_subtitles/<file>"` y nunca corrige
  contra `video_script`.
- `subtitles.mode = "none"` envia `subtitle_enabled: false`.
- `subtitles.optimize` controla `subtitle_optimization_enabled`; el default es
  `true`. Con `true`, el SRT puede adaptarse a formato vertical. Con `false`,
  el SRT se respeta literal.
- Los campos legacy `video.custom_audio_file` y `video.custom_subtitle_file`
  siguen soportados. Si chocan con `audio.file` o `subtitles.file`, el wrapper
  falla salvo que resuelvan al mismo archivo.

Prioridad efectiva de subtitulos:

1. `custom_srt`, si existe.
2. `whisper`, si se pide y hay audio propio o audio generado.
3. Edge/TTS normal del core.

Importante: si `subtitles.mode = "whisper"` y
`subtitles.correction_enabled = true`, `video_script` debe ser el transcript
real del audio. Si se envia un placeholder, la correccion puede reemplazar el
texto de Whisper por ese placeholder. `custom_srt` nunca pasa por esa correccion.

## Presets de subtitulos

El wrapper puede expandir `subtitle_style_preset` antes de encolar el job. Esto
no cambia el renderer ni la API de MoneyPrinterTurbo; solo escribe en el payload
los campos visuales que MoneyPrinterTurbo ya acepta.

Ejemplo:

```json
{
  "subtitle_style_preset": "clean_center_bold",
  "subtitle_style_overrides": {
    "font_size": 76,
    "stroke_width": 4
  }
}
```

Presets disponibles:

- `clean_center_bold`: subtitulo blanco, sin fondo, borde negro, centrado,
  `font_size` 72 y `stroke_width` 3.
- `clean_center_bold_safe`: subtitulo blanco, sin fondo, borde negro, centrado,
  `font_size` 54 y `stroke_width` 2. Recomendado para 9:16 cuando el texto
  debe quedar centrado sin cortes visuales.
- `clean_bottom_bold`: igual que `clean_center_bold`, pero abajo y con
  `font_size` 66.
- `boxed_bottom`: subtitulo blanco abajo, caja negra rectangular,
  `font_size` 60 y `stroke_width` 1.
- `large_hook_center`: subtitulo blanco grande, sin fondo, borde negro,
  centrado, `font_size` 88 y `stroke_width` 4.

Aliases:

- `center_white_black_outline` apunta a `clean_center_bold`.
- `safe_center_white_black_outline` apunta a `clean_center_bold_safe`.
- `bottom_white_black_outline` apunta a `clean_bottom_bold`.

Los presets resuelven la fuente contra `--fonts-dir` usando esta preferencia:

1. `Montserrat-Bold.ttf`
2. `MontserratBold.ttf`
3. `BeVietnamPro-Bold.ttf`
4. `MicrosoftYaHeiBold.ttc`
5. `STHeitiMedium.ttc`

Montserrat no viene incluido en este repo ni se descarga. Para usarlo, Javier
debe subir un archivo licenciado/local llamado `Montserrat-Bold.ttf` a
`resource/fonts` y confirmar que el contenedor lo ve en
`/MoneyPrinterTurbo/resource/fonts/Montserrat-Bold.ttf`. No commitear archivos
de fuente externos. Si Montserrat no existe, el fallback esperado es
`BeVietnamPro-Bold.ttf`.

`subtitle_style_overrides` solo puede modificar:

```text
subtitle_position, custom_position, font_name, text_fore_color,
text_background_color, rounded_subtitle_background, font_size,
stroke_color, stroke_width
```

Si se usa `font_name` en overrides, debe ser un nombre de archivo existente
dentro de `--fonts-dir`; no se aceptan rutas. Karaoke, animaciones y otros
efectos por palabra no estan incluidos en estos presets.

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
    ],
    "subtitle_style_preset": "clean_center_bold",
    "subtitle_style_overrides": {},
    "resolved_subtitle_style": {
      "font_name": "BeVietnamPro-Bold.ttf",
      "font_size": 72,
      "rounded_subtitle_background": false,
      "stroke_color": "#000000",
      "stroke_width": 3,
      "subtitle_position": "center",
      "text_background_color": false,
      "text_fore_color": "#FFFFFF"
    }
  },
  "video_subject": "La importancia de escoger bien a tu pareja",
  "video_script": "Escoger bien a tu pareja puede cambiar por completo el rumbo de tu vida.",
  "video_aspect": "9:16",
  "subtitle_position": "center",
  "font_name": "BeVietnamPro-Bold.ttf",
  "text_fore_color": "#FFFFFF",
  "text_background_color": false,
  "rounded_subtitle_background": false,
  "font_size": 72,
  "stroke_color": "#000000",
  "stroke_width": 3,
  "video_source": "local",
  "video_materials": [
    { "provider": "local", "url": "clip-01.mp4", "duration": 0 }
  ]
}
```

`selectedAssets` queda dentro de `runner`, no como key raiz. Esto importa porque
`nightly_runner.py` elimina `job_id`, `description`, `notes` y `runner` antes de
enviar el payload final a MoneyPrinterTurbo.

`audio` y `subtitles` tampoco quedan como keys raiz. Su metadata queda dentro de
`runner.audio` y `runner.subtitles`.

`subtitle_style_preset` y `subtitle_style_overrides` tampoco quedan como keys
raiz del job pendiente. La metadata de estilo se conserva dentro de `runner`.

La escritura en `pending/` es atomica: primero se crea un archivo temporal en la
misma carpeta y luego se reemplaza por el JSON final.
