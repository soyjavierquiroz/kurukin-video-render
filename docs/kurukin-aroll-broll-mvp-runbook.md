# Kurukin A-roll / B-roll MVP Runbook

## Estado

- Stable branch: custom/mvp
- Latest stable checkpoint: mvp-source-provider-env-wiring-2026-07-09
- Current feature pending merge: local env gitignore
- MVP técnico: completo
- E2E runner: completo
- UI E2E: completo
- Multi B-roll E2E: completo
- Prepare B-roll UI: completo
- Pexels source adapter: merged
- Source provider env wiring: merged
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

KURUKIN_ENABLE_PEXELS_SOURCE

- default unset/off
- permite preparar Pexels solo con integracion controlada
- no renderiza, no encola y no ejecuta runner por si mismo

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
- "Preparar B-roll" materializa/lista assets locales para `b_roll.assets`.
- Preparar B-roll no encola render, no ejecuta runner y no ejecuta ffmpeg.
- `local_only` es el modo seguro inicial: usa solo candidatos locales.
- `open_sources` no llama Pexels real desde UI; solo podria completar con un
  downloader controlado cuando `KURUKIN_ENABLE_PEXELS_SOURCE=1`.
- Con el flag apagado, si faltan locales, la consola muestra:
  `No hay suficientes assets locales. Pexels no está activo en esta consola.`
- El enqueue/render sigue siendo un paso separado y protegido por flags.
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
- `open_sources` usa candidatos locales primero y completa con adapters
  genericos inyectados, filtrados y ordenados por `allowed_sources`.
- Los paths de multiples fuentes se combinan/deduplican antes de construir
  `b_roll.assets`; Pexels no es fuente unica ni default.
- `local_only` solo usa candidatos locales.
- `exclusive_brand_assets` usa manifest local de marca y bloquea fuentes
  abiertas.
- Tests usan fakes; no hay Pexels real, descargas reales ni Asset Hub API.
- No se conecta a runner/render en esta fase.
- Ver: `docs/kurukin-asset-materializer.md`.

## Pexels source adapter

- Introducido como helper puro en `app/custom/pexels_source.py`.
- Usa endpoint `/v1/videos/search` y header `Authorization` directo sin
  `Bearer`.
- Selecciona MP4, prefiere portrait y deduplica por video/link.
- Guarda metadata de atribucion cuando esta disponible.
- Solo escribe bajo `storage/local_videos` o `storage/local_assets`.
- Pexels es solo un adapter de `open_sources`.
- No reemplaza `local_library`, `uploaded`, Asset Hub/manifest ni otros
  adapters futuros.
- El materializer combina/deduplica fuentes permitidas por `allowed_sources`.
- Renderer y runner siguen sin proveedores externos.
- Ver: `docs/kurukin-pexels-source-adapter.md`.

## Prepare B-roll UI

- Render Console agrega "Preparar B-roll" dentro de Presentador + B-roll.
- La accion guarda en session state los assets preparados, contador,
  `source_provider`, policy label y query.
- Si el resultado es OK, esos paths alimentan el campo B-roll local del enqueue
  UI, pero no crean pending automaticamente.
- Con flags apagados, `local_only` puede correr porque solo valida/materializa
  paths locales.
- Marca exclusiva requiere manifest local o bundle resoluble a manifest local;
  no llama Asset Hub API.
- Ver: `docs/kurukin-aroll-broll-prepare-broll-ui.md`.

## Guardrails

- MPT no llama Asset Hub API para A-roll/B-roll
- renderer no llama Pexels ni proveedores externos
- materializer solo usa proveedores inyectados/fakes en tests
- Pexels source default apagado y sin llamadas reales en esta fase
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
- antes de crear `.env`, confirmar `git check-ignore -v .env`
- `.env.example` debe contener solo placeholders vacios

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
