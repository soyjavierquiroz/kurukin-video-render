# Kurukin Render Console Plan

## Idea

Kurukin Render Console sera una interfaz paralela propia para operar MoneyPrinterTurbo como render worker. No debe copiar la UI visual original de Streamlit; debe copiar las capacidades importantes y exponerlas con un modelo mental Kurukin: assets, guion, audio, subtitulos, render, estado y auditoria.

MoneyPrinterTurbo queda como motor compatible. Kurukin Render Console queda como capa de producto y orquestacion.

## MoneyPrinterTurbo como render worker

El worker expone `/api/v1/videos`, `/api/v1/tasks`, `/api/v1/video_materials` y artefactos en `storage/tasks`. La consola no necesita tocar internals para el MVP si usa un MoneyPrinterTurbo Adapter que traduzca modelos Kurukin a payloads nativos.

Responsabilidades del worker:

- TTS.
- Whisper/Edge subtitles.
- Busqueda/descarga Pexels, Pixabay, Coverr.
- Preprocesado de local videos/images.
- Concatenacion de clips.
- Montaje de audio.
- Burn-in de subtitulos.
- Estado basico del task.

Responsabilidades Kurukin:

- UI propia.
- Validacion previa mas clara.
- Catalogo/seleccion de assets.
- Presets visuales.
- Reglas de compatibilidad.
- Jobs programados.
- Historial y diagnostico.
- Futuro soporte audio propio + SRT propio.

## Kurukin Asset Hub futuro

Kurukin Asset Hub debe ser un catalogo externo al core de MoneyPrinterTurbo. No deberia requerir que MoneyPrinterTurbo conozca taxonomias, labels, campanas, marcas o intenciones creativas. El Adapter convierte una seleccion del catalogo en `video_materials` compatibles.

Datos probables:

- filename/path seguro.
- label humano.
- duracion.
- resolucion/aspect.
- tags.
- campaign/use-case.
- orden recomendado.
- licencia/origen.
- preview.

## Modos visuales

### `pexels`

Usa `video_source = "pexels"`. Requiere keywords. Puede usar `video_terms` explicitos o pedir generacion IA al core. Riesgo principal: dependencia de API key y resultados no deterministas.

### `local_manual`

Usa `video_source = "local"` y `video_materials` explicitos. Es el modo mas controlable para Kurukin MVP. El wrapper actual ya convierte `selectedAssets` a `video_materials`.

### `own_catalog` futuro

Seleccion desde Kurukin Asset Hub. Internamente sigue enviando `video_source = "local"` y filenames resolubles por el worker, o bien una etapa previa sincroniza assets al directorio local.

### `mixed` futuro

Mezcla catalogo propio con Pexels/Pixabay/Coverr. Requiere decidir si el Adapter descarga/normaliza antes del render o si extiende el core. Recomendacion: primero resolver fuera del core y entregar una lista local final al worker.

## Modos de audio

### `TTS`

Modo nativo. Envia `video_script`, `voice_name`, `voice_rate`, `voice_volume`, `bgm_*`. Edge subtitles funcionan bien porque TTS devuelve `sub_maker`.

### `custom_audio`

Envia `custom_audio_file`. El core salta TTS, usa el audio real y calcula duracion. Para subtitulos automaticos necesita `subtitle_provider = "whisper"`. Problema actual: despues de Whisper, `subtitle.correct()` puede reemplazar texto por `video_script`.

### `custom_audio + custom_srt` futuro

Contrato recomendado. El usuario o un proceso previo aporta SRT real. El Adapter debe priorizar SRT propio, no forzar Whisper, y no corregir contra un placeholder. Puede requerir cambio formal del core o un paso controlado fuera del core que inyecte SRT al task antes de render.

## Modo simple vs modo avanzado

### Simple

Para produccion rapida:

- objetivo/tema.
- modo visual.
- seleccion de assets o proveedor.
- modo audio.
- estilo de subtitulos preset.
- boton render.

Debe esconder configuraciones peligrosas y mostrar validaciones claras: falta API key, falta asset, audio sin transcript, subtitle provider incompatible.

### Avanzado

Para control fino:

- `video_terms`.
- `match_materials_to_script`.
- `video_concat_mode`.
- `video_transition_mode`.
- `video_clip_duration`.
- `video_count`.
- `voice_name`, rate, volume.
- `bgm_type`, file, volume.
- font, size, stroke, colors, background, position.
- `n_threads`.
- prompts custom.
- diagnostico de payload final.

## Copiar capacidades sin copiar UI visual

La consola debe cubrir las capacidades funcionales de la WebUI original:

- generar guion.
- editar guion.
- generar keywords.
- elegir fuente visual.
- subir/listar materiales locales.
- elegir TTS/voice.
- usar audio propio.
- configurar BGM.
- configurar subtitulos.
- lanzar render.
- ver progreso.
- descargar/ver output.

Pero no debe copiar la composicion Streamlit de tres columnas ni su estetica. La UI Kurukin deberia ser operativa: lista de jobs, panel de payload/validacion, selector de assets, preview y estado.

## MoneyPrinterTurbo Adapter

Crear una capa `MoneyPrinterTurbo Adapter` antes de tocar core:

- Entrada: modelo Kurukin (`visual_mode`, `audio_mode`, `assets`, `subtitle_style`, metadata).
- Salida: payload MoneyPrinterTurbo limpio.
- Filtra campos propios (`runner`, `selectedAssets`, presets, notas).
- Valida incompatibilidades (`custom_audio` + Edge subtitles).
- Decide si `video_script` es guion narrativo, transcript real o placeholder prohibido.
- Persiste artifacts de diagnostico (`moneyprinter-payload.json`, response, final-task).
- Mantiene compatibilidad por version/checkpoint.

El Adapter es el lugar correcto para absorber cambios upstream: si MoneyPrinterTurbo cambia campos o endpoints, se actualiza una capa, no toda la UI Kurukin.

## Fases recomendadas

### Fase 1: Consola MVP sobre API actual

- UI propia minima para jobs.
- Modo `local_manual`.
- Modo TTS.
- Presets de subtitulos existentes.
- Submit/poll via `/api/v1/videos` y `/api/v1/tasks/{task_id}`.
- Sin tocar core.

### Fase 2: Audio propio seguro

- Exponer `custom_audio`.
- Validar `subtitle_provider`.
- Advertir si `video_script` no es transcript real.
- Permitir render sin subtitulos o con transcript pegado manualmente.
- Documentar claramente el riesgo `subtitle.correct()`.

### Fase 3: SRT propio

- Agregar contrato `custom_srt`.
- Decidir implementacion: Adapter pre/post proceso o core opt-in.
- Tests con SRT fixture.
- No depender de `video_script` para subtitulos cuando hay SRT propio.

### Fase 4: Asset Hub

- Catalogo propio.
- Tags y previews.
- Seleccion ordenada.
- Export a `video_materials`.
- Historial de assets usados por render.

### Fase 5: Mixed mode y upstream hardening

- Combinar catalogo propio + stock.
- Check automatico de upstream con `scripts/check_upstream_updates.py`.
- Matriz de compatibilidad por tag.
- Smoke tests del Adapter contra OpenAPI.

Recomendacion de arquitectura: mantener MoneyPrinterTurbo lo mas stock posible y mover producto/criterio Kurukin hacia Adapter + Console + Asset Hub. Tocar core solo cuando haya una capacidad imposible desde API, como soporte formal de SRT propio o bypass seguro de correccion Whisper.
