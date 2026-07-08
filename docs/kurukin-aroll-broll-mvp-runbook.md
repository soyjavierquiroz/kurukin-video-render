# Kurukin A-roll / B-roll MVP Runbook

## Estado

- Stable branch: custom/mvp
- Latest checkpoint: mvp-aroll-broll-results-queue-polish-2026-07-08
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

## Guardrails

- MPT no llama Asset Hub API para A-roll/B-roll
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

1. UI enable controlado para encolar A-roll/B-roll desde Render Console.
2. Demo flow con flags temporales.
3. Mejor preview metadata.
4. Soportar B-roll múltiples.
5. Segundo layout: broll_fullscreen_speaker_bubble.
6. Subtítulos desde audio A-roll.
7. Auto crop/face tracking después.
