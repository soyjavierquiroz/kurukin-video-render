# MoneyPrinterTurbo Core Flow

Fecha de inspeccion: 2026-07-03. Rama de trabajo: `feature/document-mpt-core-flow`, basada en `custom/mvp` commit `af190f3` tag `mvp-subtitle-style-presets-2026-07-03`.

Este documento describe el flujo real del core inspeccionado sin modificar comportamiento. La conclusion principal para Kurukin es que MoneyPrinterTurbo ya sirve bien como render worker, pero el contrato `video_script` sigue siendo central incluso cuando se usa audio propio. Con `subtitle_provider = "whisper"`, Whisper transcribe el audio real, pero despues `subtitle.correct()` intenta alinear el SRT contra `video_script` y puede reemplazar el texto transcrito por el guion enviado.

## Resumen del core

La entrada principal del API para video completo es `POST /api/v1/videos`, definido en `app/controllers/v1/video.py`. Crea un `task_id`, guarda estado inicial y encola `app.services.task.start()` en el task manager. El modelo de payload es `TaskVideoRequest`, derivado de `VideoParams` en `app/models/schema.py`.

El pipeline en `app/services/task.py` es lineal:

1. `generate_script()`: usa `params.video_script` si viene informado; si no, llama a `llm.generate_script()`.
2. `generate_terms()`: solo para fuentes no locales; usa `params.video_terms` si viene informado; si no, llama a `llm.generate_terms()`.
3. `save_script_data()`: escribe `storage/tasks/<task_id>/script.json`.
4. `generate_audio()`: usa TTS o `custom_audio_file`.
5. `generate_subtitle()`: usa SRT propio, Edge/TTS `sub_maker` o Whisper segun
   el payload; si el payload no define provider, usa `config.toml`.
6. `get_video_materials()`: usa materiales locales o descarga proveedor externo.
7. `generate_final_videos()`: concatena clips, monta audio, quema subtitulos y actualiza estado final.
8. Opcional: cross-posting si `upload_post` esta configurado.

## Idea/topic a guion

`VideoParams.video_subject` es obligatorio. `VideoParams.video_script` es opcional. En `task.generate_script()`, si `video_script.strip()` no esta vacio, el core no llama a IA y usa ese texto tal cual. Si esta vacio, llama a `llm.generate_script(video_subject, video_language, paragraph_number, video_script_prompt, custom_system_prompt)`.

La WebUI tiene botones separados para generar guion y keywords. Desde API, el mismo `POST /api/v1/videos` puede recibir un guion ya resuelto o solo tema para que el backend genere guion.

## Guion a keywords/search terms

Para `video_source != "local"`, `task.start()` llama a `generate_terms()`. Si `video_terms` viene informado, lo normaliza desde string separado por comas o desde lista. Si no viene informado, usa `llm.generate_terms()`.

`llm.generate_terms()` pide terminos en ingles para stock video. Si `match_materials_to_script` es `false`, genera un set general de 5 terminos por defecto. Si es `true`, genera mas terminos en orden narrativo y `task.generate_terms()` evita el reranking de TwelveLabs para preservar ese orden.

Para `video_source == "local"`, no se generan terms. En el task analizado `search_terms` quedo como string vacio porque los materiales fueron locales.

## Keywords a Pexels u otros materiales

`app/services/material.py` decide el proveedor en `download_videos()`:

- `pexels`: usa `search_videos_pexels()`, requiere `pexels_api_keys`, llama `https://api.pexels.com/videos/search` con `query`, `per_page=20` y `orientation`.
- `pixabay`: usa `search_videos_pixabay()`, requiere `pixabay_api_keys`, llama `https://pixabay.com/api/videos/`.
- `coverr`: usa `search_videos_coverr()`, requiere `coverr_api_keys`, busca en Coverr y usa `urls.mp4_download`.

Luego descarga con `save_video()` hacia `storage/cache_videos` por defecto, o a `material_directory` si esta configurado. Si `material_directory = "task"`, descarga dentro del directorio del task.

Cuando `match_materials_to_script` esta apagado, combina candidatos de todos los terms, deduplica por URL, puede mezclar aleatoriamente segun `video_concat_mode`, y descarga hasta cubrir la duracion de audio. Cuando esta encendido, `_download_videos_by_script_order()` agrupa por term y descarga en rondas para aproximar el orden narrativo.

## Local videos con video_materials

Si `video_source == "local"`, `task.get_video_materials()` llama a `video.preprocess_video(materials=params.video_materials, clip_duration=params.video_clip_duration)`.

`video.preprocess_video()` restringe los paths al directorio `storage/local_videos`. Acepta videos e imagenes (`mp4`, `mov`, `avi`, `flv`, `mkv`, `jpg`, `jpeg`, `png`), descarta material ilegible y descarta assets con resolucion menor a 480x480. Devuelve rutas locales validas.

La WebUI puede subir archivos locales y guardarlos en `storage/local_videos`. Nuestro `scripts/local_job_wrapper.py` tambien genera payloads locales: valida `selectedAssets`, ordena por `order`, aplica presets de subtitulos, fija `video_source = "local"` y produce `video_materials = [{"provider": "local", "url": <filename>, "duration": 0}]`.

## TTS/voice

Si no hay `custom_audio_file`, `generate_audio()` llama a `voice.tts()` con:

- `text=video_script`
- `voice_name=voice.parse_voice_name(params.voice_name)`
- `voice_rate=params.voice_rate`
- `voice_file=storage/tasks/<task_id>/audio.mp3`

El `sub_maker` devuelto por TTS es importante para `subtitle_provider = "edge"`, porque contiene la linea de tiempo generada por Edge/Azure u otros motores compatibles. El audio duration de TTS se redondea hacia arriba con `math.ceil(voice.get_audio_duration(sub_maker))`.

## custom_audio_file

`VideoParams.custom_audio_file` esta en el schema con comentario explicito: si existe, ignora TTS y puede seguir usando Whisper para subtitulos.

`task.resolve_custom_audio_file()` primero intenta resolver el path dentro del task dir. Si no se puede, permite un archivo server-side dentro del proyecto para paths relativos, por ejemplo `storage/local_audios/audio-prueba.mp3`. Rechaza paths relativos que escapen del repo y rechaza archivos inexistentes.

Cuando `custom_audio_file` es valido, `generate_audio()`:

- loguea `using custom audio file`
- calcula duracion con `voice.get_audio_duration(custom_audio_file)`
- devuelve `(custom_audio_file, audio_duration, None)`

El `None` es clave: no hay `sub_maker`. Por eso Edge no puede generar subtitulos para audio propio; Whisper si puede.

## custom_subtitle_file y control de correccion

Feature `custom-audio-subtitle-contract` agrega tres campos core a
`VideoParams`:

- `custom_subtitle_file`: SRT propio. Puede ser task-local o relativo seguro
  dentro del repo, por ejemplo `storage/local_subtitles/audio-prueba.srt`.
- `subtitle_correction_enabled`: default `true` para preservar el
  comportamiento original. En Whisper, `false` evita que `subtitle.correct()`
  reemplace la transcripcion real con `video_script`.
- `subtitle_optimization_enabled`: default `true` para preservar el hook
  `app.custom.subtitle_optimizer`. En `false`, el SRT queda literal.

Cuando `custom_subtitle_file` viene informado, `generate_subtitle()` lo resuelve,
lo copia al task como `subtitle.srt`, no llama a `subtitle.correct()` y solo
ejecuta el optimizer si `subtitle_optimization_enabled` esta activo.

`subtitle_provider` tambien puede venir por job. Si el payload envia
`subtitle_provider = "whisper"` o `"edge"`, `task.resolve_subtitle_provider()`
usa ese valor para el job actual. Si falta o viene vacio, el core conserva el
comportamiento historico y lee `subtitle_provider` desde `config.toml`.
`custom_subtitle_file` tiene prioridad absoluta sobre ese provider.

Cuando se usa Whisper, el core sigue llamando a `subtitle.create()` sobre el
audio real. Luego solo corrige contra `video_script` si
`subtitle_correction_enabled` es `true`. Esto evita el bug diagnosticado donde
un `video_script` placeholder reemplazaba el texto Whisper correcto.

Con Edge/TTS normal, el comportamiento se mantiene: se genera SRT desde
`sub_maker` y despues se aplica el optimizer si esta activo.

## Subtitles con Edge

`subtitle_provider` puede venir en el payload del job. Si falta, se lee desde
`config.app["subtitle_provider"]`. El default en `config.example.toml` es
`"edge"`.

Con `subtitle_provider = "edge"`, `generate_subtitle()` requiere `sub_maker`. Si `sub_maker is None` y el provider no es Whisper, retorna `""` y salta subtitulos. Ese fue el caso del primer intento de audio propio que quedo sin subtitulos: audio propio implica no TTS, no `sub_maker`, y Edge se salta.

Si hay `sub_maker`, llama a `voice.create_subtitle(text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path)`. Ese camino genera SRT desde la linea de tiempo TTS y usa `video_script` como texto canonico.

## Subtitles con Whisper

Con `subtitle_provider = "whisper"`, `generate_subtitle()` llama a `subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)`. `app/services/subtitle.py` carga `faster_whisper`, transcribe con `word_timestamps=True` y `vad_filter=True`, detecta idioma y escribe un SRT inicial.

Si `subtitle_correction_enabled` queda en su default `true`, inmediatamente despues el core llama a:

```python
subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)
```

Esta correccion no es una validacion pasiva. Si una linea del SRT no coincide con la linea esperada de `video_script`, escribe `script_line` en el SRT nuevo, conservando timestamps del subtitulo o grupo de subtitulos. Incluso en mismatch fuerte, el branch `else` agrega `script_line`. Por eso Whisper puede transcribir correctamente y aun asi el archivo final mostrar texto del guion/placeholder.

Si `subtitle_correction_enabled` es `false`, se conserva el texto Whisper literal.
Despues corre nuestro hook `app.custom.subtitle_optimizer.optimize_srt_file()`,
salvo que `subtitle_optimization_enabled` sea `false`. El optimizer puede partir
lineas largas y crear `subtitle.original.srt`; no recupera la transcripcion
Whisper anterior si `subtitle.correct()` ya la reemplazo.

## Relacion entre video_script y subtitulos

`video_script` tiene tres roles:

- Es el texto fuente para generar TTS.
- Es el contexto para generar keywords cuando no se envian `video_terms`.
- Es el texto canonico para la correccion de subtitulos Whisper.

Eso significa que, hoy, audio propio sin transcripcion exacta en `video_script` es un modo ambiguo. Si el `video_script` es placeholder, los subtitulos finales pueden terminar siendo placeholder. Si el `video_script` es el transcript exacto del audio, la correccion puede ayudar a limpiar errores menores de Whisper.

## Que necesita audio propio bien formado

Para audio propio confiable en el core actual:

- `custom_audio_file` debe apuntar a un archivo existente dentro del repo o dentro del task.
- `subtitle_provider` debe ser `"whisper"` si se quieren subtitulos automaticos.
  Ahora puede enviarse por job, sin tocar `config.toml`.
- `subtitle_correction_enabled = false` debe usarse cuando `video_script` es
  placeholder y se quiere conservar Whisper real.
- `custom_subtitle_file` debe usarse cuando ya existe un SRT propio. Ese camino
  tiene prioridad sobre Whisper y Edge, y nunca pasa por `subtitle.correct()`.
- `subtitle_provider = "edge"` por job funciona para el flujo normal con TTS y
  `sub_maker`.
- `subtitle_optimization_enabled = false` debe usarse cuando el SRT propio ya
  esta final y debe respetarse literal.

## Puntos donde MoneyPrinterTurbo usa IA

- `llm.generate_script()`: genera guion desde tema, idioma, numero de parrafos y prompts custom.
- `llm.generate_terms()`: genera keywords/search terms para Pexels/Pixabay/Coverr.
- `llm.generate_social_metadata()`: genera title/caption/hashtags para cross-posting.
- `subtitle.create()`: usa faster-whisper para speech-to-text.
- TTS providers en `voice.py`: Edge/Azure y proveedores compatibles para sintetizar audio desde texto; algunos no son LLM, pero si son IA de voz.
- TwelveLabs opcional: `twelvelabs.rerank_terms_by_subject()` puede reordenar terms por relevancia multimodal.

## Puntos donde Kurukin extendio

- `scripts/nightly_runner.py`: cola filesystem para enviar jobs al API, esperar estado y guardar artifacts.
- `scripts/local_job_wrapper.py`: adapter de specs locales a payload MoneyPrinterTurbo, con validacion de assets locales.
- `scripts/subtitle_style_presets.py`: presets de estilo para subtitulos.
- `app.custom.subtitle_optimizer`: hook cargado desde `task.generate_subtitle()` para dividir/optimizar SRT.
- Runtime local app mount: compose local monta codigo app para desarrollo sin rebuild completo.

## Subtitulos: contenido vs look visual

El contrato `custom_audio_file`, `custom_subtitle_file`,
`subtitle_correction_enabled` y `subtitle_optimization_enabled` controla que
texto llega al SRT final. El look visual se aplica despues, en
`app/services/video.py`, cuando MoviePy convierte cada item SRT en `TextClip`.
Son capas separadas: una prueba puede tener contenido correcto y aun asi fallar
visualmente por fuente, tamano, stroke o padding del renderer.

Para 9:16, `clean_center_bold_safe` usa una combinacion mas conservadora
(`font_size` 54, `stroke_width` 2) y el renderer agrega margen transparente al
texto sin fondo para que trazos, acentos y descenders no queden cortados por el
canvas interno de `TextClip`.

## Riesgos detectados

- Riesgo alto: `subtitle.correct()` sobreescribe texto Whisper con `video_script` incluso cuando el mismatch confirma que no coinciden.
- Riesgo alto: `custom_audio_file` salta TTS, por lo que Edge no puede producir subtitulos.
- Riesgo medio: `video_script` sigue siendo requerido por nuestra capa local wrapper, aunque para audio propio podria ser solo metadata o placeholder.
- Riesgo medio mitigado: `subtitle_provider` ahora puede venir por request; si
  falta, sigue siendo global via `config.toml`.
- Riesgo medio: los jobs Kurukin deben filtrar metadata propia; campos como `runner`, `selectedAssets` y presets no pertenecen al API original.
- Riesgo operativo: Pexels/Pixabay/Coverr dependen de keys locales y red; fallos pueden dejar tareas en `state=4` si una excepcion ocurre en background.

## Diagnostico del task 813d7fc3-5893-4da8-b997-633984836c50

Task: `813d7fc3-5893-4da8-b997-633984836c50`.

Estado por API: completo (`state=1`, `progress=100`). Video final: `storage/tasks/813d7fc3-5893-4da8-b997-633984836c50/final-1.mp4`. `ffprobe` reporta video 1080x1920 de 76.00s, audio AAC de 74.62s. El audio original `storage/local_audios/audio-prueba.mp3` dura 74.624s, por lo que el render uso el audio propio real.

`script.json` muestra:

- `video_subject`: `Metodo PAUSA`
- `video_script`: `Audio propio subido por Javier. Esta prueba usa videos propios, audio propio, subtitulos transcritos con Whisper y estilo clean_center_bold.`
- `video_source`: `local`
- `custom_audio_file`: `storage/local_audios/audio-prueba.mp3`
- `search_terms`: `""`
- seis materiales locales.

Los logs muestran dos runs relevantes:

- 15:42:16: con audio propio y provider Edge, se salta subtitulos porque `sub_maker` falta.
- 15:50:51: con audio propio y provider Whisper, carga `large-v3`, detecta idioma `es` con probabilidad 1.00 y transcribe frases reales del audio como "Espera", "Yo se que tienes el celular en la mano", "no estas loca", etc.

Luego aparece `## correcting subtitle` y warnings de mismatch:

- Script: `Audio propio subido por Javier`; Subtitle: `Espera ... Yo se que tienes el celular en la mano`
- Script: `Esta prueba usa videos propios`; Subtitle: `Se que quiza ya abriste el chat ...`
- Script: `audio propio`; Subtitle: `Y no`
- Script: `subtitulos transcritos con Whisper...`; Subtitle: `no estas loca no eres intensa`

Despues, `Subtitle corrected`. El `subtitle.srt` final contiene el texto placeholder/script, no la transcripcion real del audio. `subtitle.original.srt` es el backup creado por el optimizer despues de la correccion, por lo que tambien contiene texto del script corregido, solo sin el split final de lineas.

Conclusion: `subtitle.srt` no parece venir del Whisper real como texto final. Whisper si corrio y si transcribio el audio real, pero su salida fue sobrescrita por `subtitle.correct()` usando `video_script`. El render final usa audio propio real, pero subtitulos de texto placeholder.

## Recomendacion para siguiente feature

La siguiente feature deberia formalizar audio propio con uno de estos contratos:

1. `custom_audio_file + custom_srt_file`: si llega SRT propio, MoneyPrinterTurbo Adapter lo pasa por un camino controlado y no pide correccion contra `video_script`.
2. `custom_audio_file + subtitle_provider=whisper + skip_subtitle_correction=true`: para usar transcripcion real sin reemplazo por placeholder.
3. `custom_audio_file + video_script=transcript_real`: mantener core actual, pero exigir que el guion sea transcript real del audio.

Recomendacion: implementar primero en Kurukin Adapter una regla conservadora: cuando haya `custom_audio_file` y el usuario no aporte transcript real, no enviar placeholder como `video_script` para subtitulos sin advertencia. Luego agregar soporte formal para `custom_srt_file` y, si se decide tocar core, hacer el bypass de `subtitle.correct()` opt-in y testeado.
