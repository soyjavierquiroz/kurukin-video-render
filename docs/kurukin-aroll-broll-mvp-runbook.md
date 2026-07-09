# Kurukin A-roll / B-roll MVP Runbook

## Estado

- Stable branch: custom/mvp
- Latest checkpoint: mvp-aroll-broll-ui-e2e-pass-2026-07-08
- MVP técnico: completo
- E2E runner: completo
- UI E2E: completo
- Demo desde Render Console: validado con smoke-004
- E2E runner PASS: aroll-broll-runner-smoke-003
- Render mode: render_mode=aroll_broll
- Layout MVP: alternating_fullscreen

## Regla principal

El A-roll manda:

- audio final = audio original del A-roll
- duración final = duración del A-roll
- B-roll = visual support, muted/no mapeado
- output = vertical 9:16
- subtitles = none/custom/desde A-roll en fases futuras

## Flujo técnico validado

Crear/validar config
-> pending protegido
-> runner real
-> handler A-roll/B-roll
-> renderer directo interno
-> ffmpeg
-> storage/tasks/<task_id>/final-1.mp4
-> completed
-> final-task.json
-> Resultados
-> Preview/download

## Flags

KURUKIN_ENABLE_AROLL_BROLL_QUEUE

- default unset/off
- permite crear pending A-roll/B-roll solo en pruebas controladas

KURUKIN_ENABLE_AROLL_BROLL_RENDERER

- default unset/off
- con off, runner rechaza antes de API
- con on, runner usa renderer directo interno

KURUKIN_ENABLE_AROLL_BROLL_DIRECT_RENDER

- solo para smoke directo
- no necesario para runner handler

KURUKIN_ENABLE_UI_RUNNER

- default unset/off
- no fue usado para E2E A-roll/B-roll

## Artefactos PASS

Direct render PASS:

- task_id: aroll-broll-direct-smoke-002
- output: storage/tasks/aroll-broll-direct-smoke-002/final-1.mp4
- duración: 6.000000
- resolución: 720x1280
- video: h264
- audio: aac

Runner E2E PASS:

- task_id: aroll-broll-runner-smoke-003
- output: storage/tasks/aroll-broll-runner-smoke-003/final-1.mp4
- duración: 6.000000
- resolución: 720x1280
- video: h264
- audio: aac
- completed dir:
  storage/nightly_jobs/completed/20260708-164846-aroll-broll-runner-smoke-003-20260708T164854Z-1560774-1783529334006377018

## UI E2E PASS

- task_id: aroll-broll-ui-smoke-004
- checkpoint: mvp-aroll-broll-ui-e2e-pass-2026-07-08
- merge: 1d202aca785227192d09023fd0f9d36ecb29bafa
- helper UI:
  app.custom.kurukin_render_console.enqueue_aroll_broll_from_console
- output: storage/tasks/aroll-broll-ui-smoke-004/final-1.mp4
- completed dir:
  storage/nightly_jobs/completed/20260708-204354-aroll-broll-ui-smoke-004-20260708T204415Z-1713769-1783543455820704828
- duración: 6.000000s
- resolución: 720x1280
- video: h264
- audio: aac
- Render Console:
  - Cola/Resultados detectan smoke-004
  - Presentador + B-roll visible
  - Audio: A-roll original visible
  - B-roll muted visible
  - preview/download OK
- Guardrails:
  - sin /api/v1/videos
  - sin UI runner
  - flags apagados al final

## Render Console

- Cola muestra “Presentador + B-roll”
- Resultados muestra “Presentador + B-roll”
- Metadata visible:
  - Layout: alternating_fullscreen
  - Audio: A-roll original
  - B-roll muted
  - Task ID
- Preview/download funcionan desde storage/tasks
- Listar resultados no ejecuta runner/render/API

## Multiple B-roll assets

- El MVP acepta entre 1 y 8 paths B-roll locales.
- Render Console acepta uno o varios paths, uno por linea.
- Los assets se rotan en orden dentro de `alternating_fullscreen`.
- El audio final sigue siendo el A-roll original y el audio B-roll no se mapea.
- La duracion final sigue clampleada a la duracion del A-roll.
- No existe seleccion semantica en esta fase.
- No se llama Asset Hub API.

## Asset source policy

- Introducida como metadata/schema/helper para A-roll/B-roll.
- Default: `open_sources`, buscar recursos en fuentes disponibles/autorizadas.
- Marca exclusiva: `exclusive_brand_assets`, requiere
  `brand_asset_bundle_uid` y usa solo Asset Hub/manifest local de marca.
- Local: `local_only`, solo assets locales o subidos.
- Cola/Resultados pueden mostrar `Fuentes: abiertas`,
  `Fuentes: marca exclusiva` o `Fuentes: locales` cuando existe metadata.
- El renderer no decide fuentes, no llama Pexels, no llama Asset Hub API y
  consume assets locales ya materializados en `b_roll.assets` o manifest local.
- Ver: `docs/kurukin-asset-source-policy.md`.

## Asset materializer

- Introducido como helper puro en `app/custom/asset_materializer.py`.
- Convierte `asset_policy` + request en `b_roll_assets` locales.
- `open_sources` usa candidatos locales primero y completa solo con downloader
  inyectado.
- `local_only` solo usa candidatos locales.
- `exclusive_brand_assets` usa manifest local de marca y bloquea fuentes
  abiertas.
- Tests usan fakes; no hay Pexels real, descargas reales ni Asset Hub API.
- No se conecta a runner/render en esta fase.
- Ver: `docs/kurukin-asset-materializer.md`.

## Guardrails

- MPT no llama Asset Hub API para A-roll/B-roll
- renderer no llama Pexels ni proveedores externos
- materializer solo usa proveedores inyectados/fakes en tests
- no /api/v1/videos para render_mode=aroll_broll
- no DB
- no rclone
- no credenciales
- no config.toml
- no resource/fonts
- no stagear storage
- no borrar outputs
- runner real solo con autorización explícita
- ffmpeg real solo con autorización explícita

## Troubleshooting

1. Runner rechaza job:
   - revisar KURUKIN_ENABLE_AROLL_BROLL_RENDERER
   - error esperado con flag off:
     “A-roll/B-roll renderer execution is disabled”

2. Pending no se crea:
   - revisar KURUKIN_ENABLE_AROLL_BROLL_QUEUE
   - validar paths A-roll/B-roll bajo roots permitidos

3. Output demasiado largo:
   - revisar que plan tenga aroll_duration_seconds
   - timeline y -t deben clamplear a duración A-roll

4. No aparece en Resultados:
   - revisar final-task.json
   - revisar storage/tasks/<task_id>/final-1.mp4
   - revisar completed job metadata/render_mode/task_id

5. API call accidental:
   - FAIL
   - render_mode=aroll_broll no debe llamar /api/v1/videos

## Siguiente fase recomendada

Sin sobreingeniería:

1. Demo controlado con contenido real corto.
2. B-roll múltiple local.
3. Mejor metadata/preview.
4. Subtítulos desde A-roll.
5. Segundo layout: broll_fullscreen_speaker_bubble.
