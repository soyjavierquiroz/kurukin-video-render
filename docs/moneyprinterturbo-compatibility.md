# MoneyPrinterTurbo Compatibility Notes

Fecha de inspeccion: 2026-07-03. Rama de trabajo: `feature/document-mpt-core-flow`.

## Baseline local

- Base estable: `custom/mvp`.
- Commit local actual inspeccionado: `af190f3ba8627eed2e1dcca40d43323d8c3c9c8e`.
- Tag exacto: `mvp-subtitle-style-presets-2026-07-03`.
- `origin`: `git@github.com:soyjavierquiroz/kurukin-video-render.git`.
- `upstream` fetch: `https://github.com/harry0703/MoneyPrinterTurbo.git`.
- `upstream` push: `DISABLED`.

No se debe trabajar sobre `main`. La rama de sync/analisis debe salir de `custom/mvp` o de un tag/checkpoint explicito.

## Endpoints que dependemos

### `POST /api/v1/videos`

Crea task de video completo. Devuelve `data.task_id`. Payload principal: `TaskVideoRequest`.

Campos criticos:

- `video_subject`
- `video_script`
- `video_terms`
- `video_aspect`
- `video_resolution`
- `video_concat_mode`
- `video_transition_mode`
- `video_clip_duration`
- `match_materials_to_script`
- `video_count`
- `video_source`
- `video_materials`
- `custom_audio_file`
- `custom_subtitle_file`
- `subtitle_provider`
- `subtitle_correction_enabled`
- `subtitle_optimization_enabled`
- `video_language`
- `voice_name`
- `voice_volume`
- `voice_rate`
- `bgm_type`
- `bgm_file`
- `bgm_volume`
- `subtitle_enabled`
- `subtitle_position`
- `custom_position`
- `font_name`
- `text_fore_color`
- `text_background_color`
- `rounded_subtitle_background`
- `font_size`
- `stroke_color`
- `stroke_width`
- `n_threads`
- `paragraph_number`
- `video_script_prompt`
- `custom_system_prompt`

### `GET /api/v1/tasks`

Lista tasks paginados con `page` y `page_size`. El runner lo usa como check practico de disponibilidad del API.

### `GET /api/v1/tasks/{task_id}`

Consulta estado. Estados usados por el runner:

- `1`: complete.
- `-1`: failed.
- `4`: processing.

En completado puede traer `videos`, `combined_videos`, `script`, `terms`, `audio_file`, `audio_duration`, `subtitle_path`, `materials`, `cross_post_results`.

### `DELETE /api/v1/tasks/{task_id}`

Borra estado y directorio `storage/tasks/<task_id>`. No usar para el task bueno `813d7fc3-5893-4da8-b997-633984836c50` hasta terminar diagnostico y resguardo.

### `GET /api/v1/video_materials`

Lista archivos en `storage/local_videos`. Devuelve `name`, `size`, `file`. En el codigo actual el controller devuelve solo filename en `file`, aunque el ejemplo OpenAPI historico muestra path absoluto.

### `POST /api/v1/video_materials`

Sube material local a `storage/local_videos`. Acepta `mp4`, `mov`, `avi`, `flv`, `mkv`, `jpg`, `jpeg`, `png`.

### `POST /api/v1/subtitle`

Genera subtitulo solamente. Usa `SubtitleRequest`; no incluye `custom_audio_file`, por lo que no sirve hoy para transcribir audio propio directo via API sin pasar por `POST /videos`.

## Campos propios Kurukin que no deben mandarse al API

El API original no debe recibir metadata de orquestacion. `scripts/nightly_runner.py` ya filtra:

- `job_id`
- `notes`
- `description`
- `runner`

El wrapper tambien elimina del payload root:

- `selectedAssets`
- `subtitle_style_preset`
- `subtitle_style_overrides`

Estos campos pueden existir en specs Kurukin, en artifacts y en UI propia, pero MoneyPrinterTurbo Adapter debe convertirlos a campos originales antes de llamar `/api/v1/videos`.

## Archivos core que tocamos o montamos

El core a proteger en sync upstream incluye:

- `app/models/schema.py`
- `app/services/task.py`
- `app/services/video.py`
- `app/services/material.py`
- `app/services/voice.py`
- `app/services/subtitle.py`
- `app/controllers/v1/video.py`
- `app/controllers/v1/llm.py`
- `webui/Main.py`
- `config.example.toml`
- compose/Dockerfiles/runtime mounts.

En esta rama ya existen patches controlados sobre core y herramientas locales;
mantenerlos visibles al comparar contra upstream.

## Patches actuales

- `custom audio/subtitle contract`: `app/models/schema.py` expone
  `custom_subtitle_file`, `subtitle_provider`,
  `subtitle_correction_enabled` y `subtitle_optimization_enabled`;
  `app/services/task.py` usa SRT propio antes de Edge/Whisper, resuelve
  `subtitle_provider` por job con fallback a `config.toml`, permite saltar
  `subtitle.correct()` y permite omitir el optimizer por request.
- `subtitle_optimizer hook`: `task.generate_subtitle()` intenta importar `app.custom.subtitle_optimizer.optimize_srt_file()` y optimiza `subtitle.srt` despues de SRT propio, Edge o Whisper cuando `subtitle_optimization_enabled` esta activo.
- `runtime local app mount`: compose local monta codigo local para iterar sin rebuild pesado.
- `local_job_wrapper`: `scripts/local_job_wrapper.py` valida specs Kurukin, assets locales, orden, preset de subtitulos y genera payload MoneyPrinterTurbo local.
- `nightly_runner`: `scripts/nightly_runner.py` procesa cola filesystem, hace submit al API, poll de estado, artifacts y lock de ejecucion.
- `subtitle_style_presets`: `scripts/subtitle_style_presets.py` resuelve presets como `clean_center_bold` y valida overrides permitidos.
- `subtitle visual padding / safe text clip`: `app/services/video.py` agrega
  margen transparente a subtitulos sin fondo para evitar cortes de stroke,
  acentos y descenders en `TextClip`; `clean_center_bold_safe` reduce tamano y
  borde para renders 9:16 centrados.
- `render quality profiles`: `app/models/schema.py` expone
  `video_resolution` y `app/services/video.py` usa `resolve_video_size()` para
  resolver el tamano final por `video_aspect` + perfil. El default vacio
  preserva 1080p; `premium_2k` es opt-in.

## Checklist para actualizar desde upstream

1. Confirmar rama y dirty tree:
   - `git branch --show-current`
   - `git status --short --untracked-files=all`
2. No stagear `config.toml`, backups ni artifacts de tasks.
3. Crear rama de sync desde `custom/mvp` o tag estable:
   - `git checkout custom/mvp`
   - `git pull --ff-only origin custom/mvp`
   - `git checkout -b chore/upstream-sync-YYYY-MM-DD`
4. Ejecutar:
   - `python3 scripts/check_upstream_updates.py --base custom/mvp --upstream upstream/main`
5. Revisar todos los archivos `HIGH RISK`.
6. Hacer merge/rebase upstream solo en rama de sync, nunca en `main`.
7. Resolver conflictos preservando los contratos Kurukin y evitando cambios innecesarios al core.
8. Correr tests minimos.
9. Hacer prueba API sin render real si el sync toca schema/controllers.
10. Taggear checkpoint antes de promover a `custom/mvp`.

## Archivos de alto riesgo ante upstream updates

El script marca como `HIGH RISK`:

- `app/models/schema.py`
- `app/services/task.py`
- `app/services/video.py`
- `app/services/material.py`
- `app/controllers/`
- `webui/`
- `requirements.txt`
- `Dockerfile`
- `docker-compose.yml`
- `config.example.toml`

Razon: ahi vive el contrato de payload, pipeline de render, resolucion final,
padding visual de subtitulos, subtitulos/audio/materiales, API, UI original,
dependencias y runtime. `app/services/video.py` es especialmente sensible
porque concentra resolucion final y padding de subtitulos.

## Tests minimos despues de upstream sync

Sin render real:

- `python3 -m py_compile scripts/check_upstream_updates.py`
- `python3 -m unittest tests/custom/test_check_upstream_updates.py`
- `python3 -m unittest tests/custom/test_local_job_wrapper.py`
- `python3 -m unittest tests/custom/test_subtitle_style_presets.py`
- `python3 -m unittest tests/custom/test_subtitle_optimizer.py`
- `git diff --check`

Smoke read-only recomendado:

- `curl -s http://127.0.0.1:18080/openapi.json > /tmp/mpt-openapi.json`
- confirmar que existen `/api/v1/videos`, `/api/v1/tasks`, `/api/v1/tasks/{task_id}`, `/api/v1/video_materials`, `/api/v1/subtitle`.

Si se tocan subtitulos/audio, agregar fixture pequeno unitario antes de render real. Si se toca render/materiales, hacer primero `stop_at` o flujo controlado antes de lanzar un video completo.

## Estrategia de ramas/tags para upstream sync

- `main`: no tocar.
- `custom/mvp`: rama estable integradora Kurukin.
- `feature/*`: ramas de feature/documentacion/experimento.
- `chore/upstream-sync-YYYY-MM-DD`: rama temporal para traer upstream.
- Tags `mvp-<feature>-YYYY-MM-DD`: checkpoints despues de validar.

Flujo recomendado:

1. Desarrollar aislado en `feature/...`.
2. Merge controlado a `custom/mvp`.
3. Tag estable.
4. Para upstream, crear rama de sync desde ultimo tag/custom.
5. Comparar con upstream, resolver, validar, taggear.

La idea es que MoneyPrinterTurbo siga actualizable desde upstream mientras Kurukin vive como adapter, runner, docs y UI paralela, no como fork visual acoplado a la WebUI original.
