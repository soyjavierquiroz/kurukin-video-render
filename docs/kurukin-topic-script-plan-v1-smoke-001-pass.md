# Kurukin topic script plan v1 smoke 001 PASS

- task_id: `kurukin-topic-plan-smoke-001`
- mode: `topic_to_video`
- commit probado: `99f54de feat: add topic script plan v1`
- input: `topic` local; sin `audio_path`; sin `video_path`; sin `visual_path` manual
- topic: `5 errores al comprar una casa usada`
- language: `es`
- duration_seconds: `45`
- format: `vertical`
- preset: `educational`
- status: `NEEDS_INPUT`
- reason: `needs_audio_or_tts`
- topic_plan.status: `NEEDS_AUDIO`
- topic_plan.reason: `needs_audio_or_tts`
- queue result: no encolado

## Script generado resumido

Guion local tipo educational con:

- hook sobre el costo de pasar por alto lo basico
- punto 1: revisar datos visibles
- punto 2: comparar opciones
- punto 3: confirmar detalles de riesgo
- cierre/CTA suave para guardar y revisar la guia

## Escenas generadas

- escena 1: hook, `9s`
- escena 2: punto 1, `9s`
- escena 3: punto 2, `9s`
- escena 4: punto 3, `9s`
- escena 5: cierre, `9s`

## Visual keywords

- `5 errores al comprar una casa usada`
- `checklist`
- `detalle`
- `explicacion`
- `errores`
- `comprar`
- `casa`
- `usada`

## Flujo validado

`topic_to_video` -> local topic planner -> script/scenes/keywords -> `needs_audio_or_tts`.

## Confirmaciones

- sin render
- sin submit MPT nativo
- sin runner
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin descargas
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
