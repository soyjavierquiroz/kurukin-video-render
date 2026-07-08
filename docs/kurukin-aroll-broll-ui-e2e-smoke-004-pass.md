# A-roll / B-roll UI E2E smoke 004 PASS

Fecha: 2026-07-08

Branch: feature/aroll-broll-ui-e2e-smoke-004

Base: 7552bb0c9b2cb92913d95650322848363247938d

Task id: aroll-broll-ui-smoke-004

## Fixtures

- A-roll: storage/local_videos/_aroll_broll_smoke_001/aroll_presenter_fixture_6s.mp4
- B-roll: storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4

## UI path

El pending se creo por el helper de Render Console llamado por la UI:

- `app.custom.kurukin_render_console.enqueue_aroll_broll_from_console`
- La UI A-roll/B-roll ahora usa ese helper desde `webui/pages/Kurukin_Render_Console.py`.
- AppTest para crear pending no fue viable en el Python host porque `streamlit` no estaba instalado; se uso el helper exacto de Render Console/UI, no el helper bajo de cola.
- AppTest post-run si se ejecuto dentro del contenedor `moneyprinterturbo-webui`, con `exception_count=0`.

## Queue

- Pending creado con `render_mode="aroll_broll"` desde UI/Render Console path.
- Pending file: storage/nightly_jobs/pending/20260708-204354-aroll-broll-ui-smoke-004.json
- Pending consumido por runner.
- Job movido a completed.
- Completed dir: storage/nightly_jobs/completed/20260708-204354-aroll-broll-ui-smoke-004-20260708T204415Z-1713769-1783543455820704828
- No quedo pending.
- No quedo failed.

Artifacts:
- submit-response.json
- final-task.json
- render-result.json
- no error.json

## Runner

Comando ejecutado una sola vez:

```bash
KURUKIN_ENABLE_AROLL_BROLL_RENDERER=1 python3 scripts/nightly_runner.py --max-jobs 1 --ignore-window --queue-dir /opt/moneyprinterturbo/storage/nightly_jobs --api-base-url http://127.0.0.1:18080/api/v1
```

Resultado:
- jobs_started=1
- sin /api/v1/videos
- sin POST http/https
- sin UI runner
- sin local_job_wrapper

## Output

- storage/tasks/aroll-broll-ui-smoke-004/final-1.mp4
- tamano: 1954406 bytes (1.9M)
- duracion: 6.000000s
- resolucion: 720x1280
- video stream: h264 presente, 30/1 fps
- audio stream: aac presente

## Render Console

- `Presentador + B-roll` visible en Cola/Resultados.
- `Audio: A-roll original` visible.
- `B-roll muted` visible.
- `aroll-broll-ui-smoke-004` detectado/listado.
- Preview visible con `Preview disponible`.
- Descarga no rota: seccion `Descargar` visible y `read_video_bytes_for_download` preparo 1954406 bytes para `final-1.mp4`.
- AppTest post-run dentro del contenedor: `exception_count=0`, sin HTML crudo, sin pending nuevo, sin task nuevo adicional.

## Guardrails

- Runner real ejecutado una sola vez.
- ffmpeg real ejecutado indirectamente una sola vez via runner handler.
- No UI runner.
- No KURUKIN_ENABLE_UI_RUNNER.
- No scripts/local_job_wrapper.py.
- No API ni /api/v1/videos.
- No Asset Hub API.
- No DB/rclone/credenciales.
- No storage stageado.
- smoke-001, smoke-002 y smoke-003 preservados.
- MP4 previos del MVP preservados.
- Output binario queda en storage y no se incluye en git.
