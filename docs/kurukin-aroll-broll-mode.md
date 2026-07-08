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

Fase actual: foundation/schema/UI skeleton.

- Helper puro en `app/custom/aroll_broll_mode.py`.
- Validacion de rutas y defaults seguros.
- Timeline conceptual sin leer video real.
- Skeleton en Render Console.
- Boton de cola deshabilitado para A-roll/B-roll.
- Tests unitarios y AppTest.

Siguiente fase: renderer `alternating_fullscreen`.

- Leer duracion real del A-roll.
- Usar audio original del A-roll como audio principal.
- Componer visualmente A-roll y B-roll.
- Conectar subtitles desde audio del A-roll o SRT.
- Habilitar enqueue solo cuando el renderer soporte este payload.

## Renderer alternating_fullscreen

El renderer core del layout `alternating_fullscreen` prepara la composicion MVP sin
activar todavia la cola desde la UI.

- El audio A-roll corre continuo y se mapea como audio final.
- El B-roll queda muted porque su audio no se mapea.
- La visual alterna entre A-roll full screen y B-roll full screen.
- El output objetivo es vertical 9:16, por defecto 720x1280.
- El crop/fit usa center crop seguro para normalizar cada segmento.
- El comando ffmpeg se construye como lista segura, sin `shell=True`.
- La fase actual implementa renderer core, ffprobe/ffmpeg helpers y dry-run.
- La cola solo crea pending protegido para A-roll/B-roll; el runner no lo ejecuta.

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
- Esta fase no ejecuta renderer real ni crea task.
- La proteccion existe hasta conectar el handler real.

Siguiente fase: conectar handler real y validar E2E con un fixture pequeno y
controlado.

## Guardrails

- MoneyPrinterTurbo no llama Asset Hub API para este modo.
- No usa rclone.
- No toca DB.
- No acepta paths arbitrarios.
- A-roll local debe vivir bajo `storage/local_videos` o `storage/local_assets`.
- Manifests de Asset Hub deben vivir bajo `/data/job-assets/<bundle_uid>/manifests/renderer-manifest.json`.
- En reposo/default no crea pending; solo puede crearlo con flag explicito de
  cola para pruebas controladas.
- La fase actual no ejecuta renderer real ni crea task.
