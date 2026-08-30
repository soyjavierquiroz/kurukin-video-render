# Kurukin A-roll/B-roll runner handler

## Flujo

El nightly runner reserva el pending job con la mecanica existente y lee
`job.json`.

- Si `render_mode` no es `aroll_broll`, el flujo normal de MoneyPrinterTurbo no
  cambia.
- Si `render_mode` es `aroll_broll`, el runner valida el flag
  `KURUKIN_ENABLE_AROLL_BROLL_RENDERER`.
- Con el flag apagado, falla antes de API y mueve el job a `failed`.
- Con el flag encendido, construye un `ArollBrollRenderPlan` desde
  `job["aroll_broll"]` y llama el renderer directo interno.
- El handler no llama `/api/v1/videos` ni `/api/v1/tasks`.

## Flags

- `KURUKIN_ENABLE_AROLL_BROLL_QUEUE`: permite crear pending A-roll/B-roll desde
  superficies controladas.
- `KURUKIN_ENABLE_AROLL_BROLL_RENDERER`: permite que el runner ejecute el
  handler directo.
- `KURUKIN_ENABLE_AROLL_BROLL_DIRECT_RENDER`: aplica al smoke script directo,
  no al handler del runner.

## Artefactos

En success:

- `submit-response.json`
- `final-task.json`
- `render-result.json`
- output esperado: `storage/tasks/<task_id>/final-1.mp4`

En failure:

- `error.json`
- `render-result.json` cuando el renderer fue invocado

`error.json` conserva `returncode`, `stdout`, `stderr` y `render_mode` cuando el
fallo viene del handler A-roll/B-roll.

## Guardrails

- No usa `shell=True`.
- No llama Asset Hub API.
- No usa rclone.
- No toca DB ni credenciales.
- No crea pending por si solo.
- Esta fase se valida con tempfile, fake duration y fake renderer.

## Siguiente fase

El siguiente paso es un E2E controlado `runner smoke 003` con un fixture pequeno,
runner real y ffmpeg real solo bajo autorizacion explicita.
