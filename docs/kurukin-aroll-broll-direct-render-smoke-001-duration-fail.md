# A-roll / B-roll direct render smoke 001 duration mismatch

Fecha: 2026-07-08

Branch: feature/aroll-broll-direct-render-real-smoke-001

Fix previo: 97fd05e fix: prepare a-roll b-roll direct render output directory

Task id: aroll-broll-direct-smoke-001

## Resultado del retry

- ffmpeg returncode=0
- ok=true
- output creado: storage/tasks/aroll-broll-direct-smoke-001/final-1.mp4
- tamano: 1,989,410 bytes
- duracion real: 9.000000s
- duracion esperada: aproximadamente 6s
- resolucion: 720x1280
- video stream: presente
- audio stream: presente

## Diagnostico

- El renderer genera MP4 valido.
- El fallo es de duracion: el output excede la duracion del A-roll.
- Causa probable: el plan del smoke usaba duracion sintetica de 12s y el
  command builder usaba la duracion maxima del timeline para `-t`, por lo que
  el filtergraph visual podia extenderse mas alla del A-roll real.

## Guardrails

- No runner.
- No scripts/nightly_runner.py.
- No scripts/local_job_wrapper.py.
- No pending.
- No API ni /api/v1/videos.
- No Asset Hub API.
- No DB/rclone/credenciales.
- Storage no stageado.
- MP4 previos preservados.
- No se repitio execute despues de detectar la duracion incorrecta.

## Fix aplicado

- El render plan puede declarar `aroll_duration_seconds`.
- El timeline se construye con duracion A-roll y clampa el ultimo segmento al
  final del A-roll.
- El command builder clampa defensivamente los segmentos del filtergraph a la
  duracion A-roll.
- El output ffmpeg fuerza `-t <aroll_duration>` antes del output path.
- El smoke dry-run expone `a_roll_duration_seconds` y
  `timeline_duration_seconds` en el JSON.
- Los tests cubren timeline clampado, ausencia de segmentos <= 0, `-t` antes
  del output path, audio A-roll `0:a?`, ausencia de audio B-roll, dry-run sin
  crear task dir, execute con fake runner y diagnostico de failure.
