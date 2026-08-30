# Kurukin MPT engine audit

Fecha: 2026-07-09

Branch: `feature/kurukin-use-mpt-engine-audit`

Base: `custom/mvp`

## Alcance

Auditoria solo lectura para alinear Kurukin con el motor nativo de
MoneyPrinterTurbo. No se contacto Pexels, Pixabay ni Coverr. No se descargaron
assets, no se ejecuto render, runner, ffmpeg ni ffprobe, y no se llamo
`/api/v1/videos`.

Los grep de auditoria quedaron en:

- `/tmp/mpt_engine_audit.txt`
- `/tmp/mpt_api_service_audit.txt`
- `/tmp/mpt_render_pipeline_audit.txt`

## 1. Native MPT sourcing

Archivos principales:

- `app/models/schema.py`
  - `MaterialInfo`
  - `VideoParams.video_source`
  - `VideoParams.video_materials`
  - `VideoParams.asset_hub_renderer_manifest_path`
- `app/services/material.py`
  - `search_videos_pexels()`
  - `search_videos_pixabay()`
  - `search_videos_coverr()`
  - `download_videos()`
  - `save_video()`
- `app/services/task.py`
  - `get_video_materials()`
  - `apply_asset_hub_renderer_manifest()`
- `app/services/video.py`
  - `preprocess_video()`
- `webui/Main.py`
  - seleccion de `video_source`
  - carga de archivos locales
  - administracion UI de keys Pexels/Pixabay/Coverr

Proveedores soportados de forma nativa:

- `pexels`
- `pixabay`
- `coverr`
- `local`

Keys/config:

- Pexels usa `config.app["pexels_api_keys"]`.
- Pixabay usa `config.app["pixabay_api_keys"]`.
- Coverr usa `config.app["coverr_api_keys"]`.
- `app.services.material.get_api_key()` rota keys y las toma de `config.toml`.
- La WebUI nativa puede guardar keys con `config.save_config()`, pero esta rama
  no toca `config.toml`.

Endpoints nativos:

- Pexels nativo usa `https://api.pexels.com/videos/search`.
- Pixabay nativo usa `https://pixabay.com/api/videos/`.
- Coverr nativo usa `https://api.coverr.co/videos`.

Resultado del sourcing:

- Las busquedas devuelven `MaterialInfo` con `provider`, `url` y `duration`.
- `download_videos()` descarga esas URLs mediante `save_video()`.
- Por defecto se escribe en `storage/cache_videos`.
- Si `config.app.material_directory == "task"`, se escribe dentro del task.
- Para `video_source="local"`, MPT no busca proveedores: consume
  `video_materials` y los valida/preprocesa en `video.preprocess_video()`.

## 2. Native MPT task model

Modelo principal:

- `app.models.schema.VideoParams`
- `app.models.schema.TaskVideoRequest`

Endpoint y servicio:

- `POST /api/v1/videos` esta en `app/controllers/v1/video.py`.
- `create_task()` crea `task_id`, actualiza state y encola
  `app.services.task.start()`.
- La WebUI nativa llama directo `tm.start(task_id=task_id, params=params)`.

Parametros principales:

- `video_subject`
- `video_script`
- `video_terms`
- `video_aspect`
- `video_resolution`
- `video_concat_mode`
- `video_clip_duration`
- `match_materials_to_script`
- `video_source`
- `video_materials`
- `custom_audio_file`
- `custom_subtitle_file`
- `subtitle_provider`
- `subtitle_enabled`
- `voice_name`, `voice_rate`, `voice_volume`
- `bgm_type`, `bgm_file`, `bgm_volume`

Script/audio/subtitles/materials:

- `generate_script()` usa `video_script` si viene informado; si no, genera con
  LLM.
- `generate_terms()` solo corre cuando `video_source != "local"`.
- `generate_audio()` usa `custom_audio_file` si existe; si no, TTS.
- `generate_subtitle()` usa `custom_subtitle_file`, Edge/TTS o Whisper segun el
  job.
- `get_video_materials()` usa materiales locales o descarga proveedor nativo.
- `save_script_data()` persiste `script.json` bajo `storage/tasks/<task_id>/`.

## 3. Native MPT renderer

Archivos principales:

- `app/services/task.py`
  - `generate_final_videos()`
  - `start()`
- `app/services/video.py`
  - `combine_videos()`
  - `generate_video()`
  - `preprocess_video()`
  - `concat_video_clips_with_ffmpeg()`
- `app/utils/utils.py`
  - `task_dir()`
  - `get_ffmpeg_binary()`

Consumo de materiales:

- Para `video_source="local"`, `preprocess_video()` valida rutas bajo
  `storage/local_videos` o, para `provider="asset_hub"`, bajo el volumen seguro
  de job assets.
- Videos locales pasan como rutas validadas.
- Imagenes locales pueden convertirse a clips temporales con motion.
- Para fuentes externas, `download_videos()` entrega paths locales descargados.

Produccion final:

- `combine_videos()` crea `storage/tasks/<task_id>/combined-<index>.mp4`.
- `generate_video()` aplica audio, BGM y subtitulos.
- El resultado final es `storage/tasks/<task_id>/final-<index>.mp4`; para el
  primer video, `final-1.mp4`.
- `state.update_task()` guarda `videos`, `combined_videos`, `script`, `terms`,
  `audio_file`, `audio_duration`, `subtitle_path` y `materials`.

## 4. Kurukin hooks correctos

Kurukin debe entrar antes del submit nativo MPT:

- mejorar concepto, guion, metadata y policy;
- decidir si el job usa proveedores nativos MPT o materiales locales;
- construir un spec compatible con `VideoParams`;
- preservar metadata Kurukin fuera del payload estrictamente MPT cuando no sea
  campo nativo;
- dejar la ejecucion a `app.services.task.start()` o al endpoint nativo solo
  cuando exista autorizacion explicita.

No debe duplicar:

- busqueda Pexels/Pixabay/Coverr;
- descarga de stock media;
- validacion/preprocesado de `video_materials`;
- composicion MoviePy/ffmpeg;
- estructura `storage/tasks/<task_id>/final-1.mp4`;
- estado de tasks nativo.

Codigo custom actual a mantener por ahora:

- `app/custom/asset_source_policy.py`: policy Kurukin.
- `app/custom/asset_materializer.py`: prepare-only local/controlado.
- `app/custom/kurukin_render_console.py`: UX y guards.
- `app/custom/kurukin_job_queue.py`: cola controlada y lectura de resultados.
- `app/custom/kurukin_job_adapter.py`: adapter existente para jobs locales.
- `app/custom/mpt_engine_bridge.py`: nuevo bridge conceptual hacia MPT.

Codigo custom a deprecar como ruta primaria:

- `app/custom/pexels_source.py`: fallback experimental/no primario.
- Nuevos adapters propios para Pixabay/Coverr: no crear hasta terminar el
  alineamiento con integraciones nativas MPT.
- Renderer paralelo A-roll/B-roll: mantener solo para gaps claros y como
  extension minima si MPT no cubre la operacion.

## 5. A-roll/B-roll strategy

Representacion propuesta:

- A-roll es el input primario del producto.
- Audio A-roll manda.
- B-roll son materiales visuales de apoyo.
- Kurukin compila esa intencion a un spec MPT primero.

Mapping nativo posible:

- Si el A-roll ya tiene audio separado, mapear a
  `VideoParams.custom_audio_file`.
- Si los B-roll ya son locales, mapear a `video_source="local"` y
  `video_materials`.
- Si se requiere stock, usar `video_source="pexels"`, `"pixabay"` o `"coverr"`
  y `video_terms`, dejando que MPT busque y descargue.
- Si se usa manifest local de marca, usar
  `asset_hub_renderer_manifest_path` para que MPT convierta a materiales
  locales.
- Subtitulos propios se mapean a `custom_subtitle_file`.

Gaps reales:

- MPT acepta `custom_audio_file`, pero no extrae de forma nativa el audio de un
  archivo A-roll video dentro de `VideoParams`.
- El pipeline nativo concatena materiales contra la duracion del audio; no
  expresa por si solo un layout editorial alternando A-roll visible y B-roll
  visible con timeline especifico.
- El soporte A-roll/B-roll completo debe extender el motor en el punto minimo:
  conservar `VideoParams`/task/result nativos, agregar solo la operacion
  faltante para audio/timeline si no puede representarse con primitives
  existentes.

## Bridge agregado

`app/custom/mpt_engine_bridge.py` agrega funciones puras:

- `discover_mpt_engine_capabilities()`
- `build_mpt_video_task_from_kurukin_job(kurukin_job)`
- `build_mpt_aroll_broll_task_spec(kurukin_job)`
- `normalize_mpt_video_params_spec(spec)`
- `validate_against_mpt_video_params(spec)`
- `build_validated_mpt_video_task_from_kurukin_job(kurukin_job)`
- `validate_mpt_task_spec(spec)`
- `summarize_mpt_task_spec(spec)`

El bridge no llama proveedores, no descarga, no renderiza, no crea pending/task
y no llama API. Produce un spec `execution="spec_only"` para revisar o someter a
MPT en una fase autorizada.

## VideoParams validation checkpoint

`mpt_engine_bridge` ahora valida los params generados contra
`app.models.schema.VideoParams` antes de cualquier submit real. La validacion:

- importa el modelo `VideoParams`;
- filtra el payload a campos reales del modelo;
- soporta Pydantic v2 con `model_validate()`;
- soporta Pydantic v1 con `parse_obj()`;
- normaliza errores a `{field, message, type}`;
- redacta mensajes que parezcan contener secretos.

Validar un spec no ejecuta render, no llama proveedores, no descarga assets, no
crea pending/task y no llama `/api/v1/videos`. El resultado es solo una prueba
de compatibilidad de schema.

Campos Kurukin que no existen en `VideoParams` quedan fuera del payload MPT y se
preservan en `kurukin_metadata`. Esto mantiene el contrato del motor nativo y
evita romper la validacion con metadata de producto.

Gaps A-roll/B-roll conocidos:

- audio desde video A-roll si no existe audio separado;
- timeline editorial alternando A-roll visible y B-roll visible.

El siguiente paso despues de este checkpoint es un submit controlado al motor
MPT solo con autorizacion explicita.
