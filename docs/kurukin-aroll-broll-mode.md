# Kurukin A-roll / B-roll mode

## Definicion

A-roll es el video principal de una persona hablando a camara. B-roll son clips o imagenes de apoyo visual que se intercalan o componen alrededor del presentador.

## Regla principal

El A-roll manda.

- El audio principal es el audio original del A-roll.
- La duracion principal es la duracion del A-roll.
- Los subtitulos salen del audio del A-roll, de un SRT propio o se desactivan.
- El B-roll es apoyo visual y queda muted por defecto.
- TTS no aplica para este modo MVP.

## Layouts planificados

- `alternating_fullscreen`
- `vertical_split_a_top`
- `vertical_split_b_top`
- `broll_fullscreen_speaker_bubble`
- `aroll_main_broll_lower_panel`

El primer layout real de renderer sera `alternating_fullscreen`.

## MVP por fases

Fase actual: visibilidad read-only en Cola y Resultados despues del E2E runner PASS.

- Helper puro en `app/custom/aroll_broll_mode.py`.
- Validacion de rutas y defaults seguros.
- Timeline conceptual sin leer video real.
- Skeleton en Render Console.
- Cola protegida por flag para A-roll/B-roll.
- Renderer directo core para `alternating_fullscreen`.
- Handler de runner protegido por flag, validado con fake renderer.
- Tests unitarios y AppTest sin runner real ni ffmpeg real.
- E2E runner PASS registrado para `aroll-broll-runner-smoke-003`.

Fase actual de polish:

- Listar jobs/resultados A-roll/B-roll sin ejecutar runner ni renderer.
- Mostrar metadata humana en Cola y Resultados.
- Mantener outputs existentes bajo `storage/tasks/<task_id>/final-1.mp4`.

## Renderer alternating_fullscreen

El renderer core del layout `alternating_fullscreen` prepara la composicion MVP sin
activar todavia la cola desde la UI.

- El audio A-roll corre continuo y se mapea como audio final.
- El B-roll queda muted porque su audio no se mapea.
- La visual alterna entre A-roll full screen y B-roll full screen.
- El output objetivo es vertical 9:16, por defecto 720x1280.
- El crop/fit usa center crop seguro para normalizar cada segmento.
- El comando ffmpeg se construye como lista segura, sin `shell=True`.
- La fase actual implementa renderer core, ffprobe/ffmpeg helpers, dry-run y
  handler de runner con fakes.
- La cola solo crea pending protegido para A-roll/B-roll con flag explicito.
- La duracion final debe ser igual a la duracion del A-roll.
- El timeline y el comando ffmpeg se clamplean a la duracion del A-roll.

## Queue integration protegida

La cola A-roll/B-roll esta protegida por `KURUKIN_ENABLE_AROLL_BROLL_QUEUE`.
En reposo/default, Render Console no crea pending A-roll/B-roll.

- En pruebas controladas, con `KURUKIN_ENABLE_AROLL_BROLL_QUEUE=1`, puede crear
  un pending protegido.
- El pending job incluye `render_mode: "aroll_broll"`.
- El pending job incluye `aroll_broll` con la config validada.
- Render Console solo encola despues de validacion estricta y flag explicito.
- La ejecucion esta protegida por `KURUKIN_ENABLE_AROLL_BROLL_RENDERER`.
- Con renderer flag off, el runner rechaza antes de llamar `/api/v1/videos`.
- Con renderer flag on, el runner usa el renderer directo interno y no llama
  `/api/v1/videos`.
- Esta fase implementa handler con tests/fakes; no ejecuta runner real ni
  ffmpeg real.

E2E runner PASS registrado: `aroll-broll-runner-smoke-003`.

UI E2E PASS registrado: `aroll-broll-ui-smoke-004`.

## Runner handler

El runner detecta `render_mode="aroll_broll"` antes del flujo normal de API.

- Con `KURUKIN_ENABLE_AROLL_BROLL_RENDERER` apagado, rechaza antes de API.
- Con `KURUKIN_ENABLE_AROLL_BROLL_RENDERER=1`, usa el renderer directo interno.
- El handler no llama `/api/v1/videos`.
- El output esperado es `storage/tasks/<task_id>/final-1.mp4`.
- Escribe `submit-response.json` con `status: 200`, `message: success` y
  `data.task_id`.
- Escribe `final-task.json` compatible con `state=1`, `progress=100` y
  `videos=["/tasks/<task_id>/final-1.mp4"]`.
- En failure escribe `error.json` con `type`, `error`, `returncode`, `stdout`,
  `stderr`, `timestamp` y `render_mode`.
- Esta fase solo valida el handler con tempfile, fake duration y fake renderer.
  El E2E real con runner queda para la siguiente rama.

## Direct render smoke

`scripts/aroll_broll_direct_render_smoke.py` prepara un smoke minimo para la fase
de direct render A-roll/B-roll.

- Por defecto corre en dry-run y no ejecuta ffmpeg.
- Valida rutas locales, arma el render plan y construye el comando como
  `list[str]`.
- El output planificado apunta a `storage/tasks/<task-id>/final-1.mp4`.
- En dry-run no crea task dir, pending job ni archivo MP4.
- `--execute` requiere `KURUKIN_ENABLE_AROLL_BROLL_DIRECT_RENDER=1`; sin ese
  flag aborta con `Direct A-roll/B-roll render execution is disabled`.
- En execute crea el directorio padre del output antes de invocar ffmpeg.
- Los failures imprimen `returncode`, `stdout` y `stderr` en el JSON para
  diagnostico.
- Una prueba real fallida no debe repetirse sin nueva autorizacion explicita.

La siguiente fase requiere autorizacion explicita para ejecutar ffmpeg real con
un fixture pequeno y controlado.

## Results and queue visibility

- Cola identifica `render_mode=aroll_broll` desde pending, completed metadata,
  `final-task.json`, `submit-response.json` o fallback por `task_id`.
- Resultados muestra `Presentador + B-roll` cuando el MP4 corresponde a un job
  A-roll/B-roll.
- La metadata visible incluye `Layout: alternating_fullscreen`,
  `Audio: A-roll original`, `B-roll muted` y `Task ID`.
- Outputs se siguen leyendo desde `storage/tasks/<task_id>/final-1.mp4`.
- No se ejecuta renderer ni runner para listar resultados.
- E2E runner PASS: `aroll-broll-runner-smoke-003`.

## Guardrails

- MoneyPrinterTurbo no llama Asset Hub API para este modo.
- No usa rclone.
- No toca DB.
- No acepta paths arbitrarios.
- A-roll local debe vivir bajo `storage/local_videos` o `storage/local_assets`.
- Manifests de Asset Hub deben vivir bajo `/data/job-assets/<bundle_uid>/manifests/renderer-manifest.json`.
- En reposo/default no crea pending; solo puede crearlo con flag explicito de
  cola para pruebas controladas.
- La fase actual no ejecuta runner real ni ffmpeg real.
