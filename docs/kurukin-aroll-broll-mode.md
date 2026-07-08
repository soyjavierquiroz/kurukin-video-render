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

## Guardrails

- MoneyPrinterTurbo no llama Asset Hub API para este modo.
- No usa rclone.
- No toca DB.
- No acepta paths arbitrarios.
- A-roll local debe vivir bajo `storage/local_videos` o `storage/local_assets`.
- Manifests de Asset Hub deben vivir bajo `/data/job-assets/<bundle_uid>/manifests/renderer-manifest.json`.
- La fase actual no ejecuta renderer real, no crea pending job y no crea task.
