# Kurukin uses MoneyPrinterTurbo engine

Fecha: 2026-07-09

## Principios

- MoneyPrinterTurbo es el motor base.
- Kurukin no reimplementa sourcing ni render.
- Kurukin orquesta intents, metadata, brand policy y UX.
- Sourcing externo usa proveedores nativos MPT cuando existen.
- Render usa pipeline nativo MPT.
- Custom renderer/adapter solo existe para gaps minimos, documentados y
  reversibles.

## Boundary

Kurukin debe producir una especificacion compatible con MPT:

- concepto del video;
- guion o transcript;
- terminos de busqueda;
- policy de marca/assets;
- seleccion de fuente;
- materiales locales cuando ya existen;
- audio/subtitulos cuando ya existen;
- metadata Kurukin para UI/observabilidad.

Kurukin no debe:

- llamar Pexels/Pixabay/Coverr si MPT puede hacerlo;
- descargar assets por su cuenta salvo prepare-only controlado;
- crear otro task/result model;
- llamar ffmpeg para renders finales fuera del motor;
- saltarse `/storage/tasks/<task_id>/final-1.mp4` como contrato de resultado;
- tocar secrets/config para resolver una integracion.

## A-roll/B-roll

A-roll/B-roll es una intencion de edicion/producto:

- A-roll es el input principal.
- El audio A-roll manda.
- B-roll son materiales de apoyo.
- B-roll no aporta audio por defecto.
- Subtitulos vienen de policy: ninguno, SRT propio o transcripcion autorizada.

La intencion debe compilarse a task/spec de MPT:

- A-roll audio separado -> `custom_audio_file`.
- B-roll local -> `video_source="local"` + `video_materials`.
- B-roll stock -> `video_source` nativo MPT + `video_terms`.
- Marca exclusiva -> manifest local via `asset_hub_renderer_manifest_path`.
- SRT propio -> `custom_subtitle_file`.

Si MPT no soporta una operacion:

- documentar el gap;
- extender el motor en el punto minimo;
- preservar task params, task state y result structure nativos;
- no crear motor paralelo.

## Nuevo bridge

`app/custom/mpt_engine_bridge.py` es el punto de compilacion conceptual.

Responsabilidades:

- descubrir capacidades nativas de MPT sin side effects;
- transformar un job Kurukin en spec MPT;
- validar `mpt_params` contra `app.models.schema.VideoParams`;
- preservar metadata Kurukin;
- validar campos faltantes;
- resumir el spec para UI/operador.

No responsabilidades:

- network;
- proveedores externos;
- descargas;
- pending;
- task real;
- API;
- runner;
- render.

## Estado de adapters custom

`app/custom/pexels_source.py` queda como fallback experimental/no primario para
prepare-only controlado. La ruta preferida para stock media es usar
`app.services.material` mediante el motor nativo MPT cuando el render real este
autorizado.

No crear adapters propios para Pixabay/Coverr hasta cerrar la integracion con el
bridge nativo MPT.

## VideoParams validation checkpoint

El bridge ahora tiene una validacion real contra
`app.models.schema.VideoParams`:

- `normalize_mpt_video_params_spec()` deja en el payload solo campos del modelo
  MPT.
- `validate_against_mpt_video_params()` usa `model_validate()` en Pydantic v2 o
  `parse_obj()` en Pydantic v1.
- `build_validated_mpt_video_task_from_kurukin_job()` compila un job Kurukin y
  valida sus params MPT sin ejecutar nada.

Esta validacion no llama proveedores, no descarga, no crea task, no crea
pending, no llama API y no renderiza. Solo demuestra que la capa Kurukin puede
producir params compatibles con el modelo nativo.

Metadata de producto como `render_mode=aroll_broll`, intencion A-roll/B-roll,
policy de assets o diagnosticos queda en `kurukin_metadata`, no dentro de
`VideoParams`, salvo que exista un campo nativo equivalente.

Gaps A-roll/B-roll aun documentados:

- MPT acepta `custom_audio_file`, pero no extrae audio desde un video A-roll si
  no hay audio separado.
- El timeline editorial alternando A-roll visible y B-roll visible no queda
  expresado solo con `video_materials`.

Siguiente paso: submit controlado al motor MPT solo con autorizacion explicita.

## Submit dry-run plan

`app/custom/mpt_engine_submitter.py` prepara un plan de submit hacia el motor
MPT usando specs ya validados contra `VideoParams`.

Esta capa mantiene el target futuro como metadata:

- `submit_target.api_path="/api/v1/videos"`
- `submit_target.service_path="app.services.task.start"`

En este checkpoint no ejecuta el target. Por defecto opera en `dry_run`, no usa
executor y no llama API, proveedores, descargas, pending, task real, runner ni
render.

Un submit no-dry-run queda bloqueado salvo que exista autorizacion explicita,
`KURUKIN_ENABLE_MPT_ENGINE_SUBMIT=1` y un executor inyectado. Esta rama no
implementa el executor real.
