# Kurukin results panel v1 smoke 001 PASS

- commit probado: `ccf9643 feat: add intent results panel`
- smoke: funcional local-only, sin render
- jobs inspeccionados:
  - `kurukin-batch-manual-process-smoke-001`
  - `kurukin-batch-manual-process-smoke-002`
- queue item paths:
  - `storage/nightly_jobs/pending/20260712-213432-kurukin-batch-manual-process-smoke-001.json`
  - `storage/nightly_jobs/pending/20260712-213432-kurukin-batch-manual-process-smoke-002.json`
- outputs detectados:
  - `storage/tasks/kurukin-batch-manual-process-smoke-001/final-1.mp4`
  - `storage/tasks/kurukin-batch-manual-process-smoke-002/final-1.mp4`
- status final:
  - `kurukin-batch-manual-process-smoke-001`: `DONE`
  - `kurukin-batch-manual-process-smoke-002`: `DONE`
- source: `job_intent_v1`
- mode: `audio_to_video`
- visual_autofill_source: `local_picker_v1`
- output_exists: `true` para ambos items

## Flujo validado

pending queue `job_intent_v1` -> `list_intent_results` -> detección de `storage/tasks/<task_id>/final-1.mp4` -> panel `Resultados de intenciones` -> preview/download protegidos por lectura local.

## Confirmaciones

- sin render
- sin submit MPT nativo
- sin runner/nightly execution
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin descargas
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
