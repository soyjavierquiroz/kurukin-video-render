# Kurukin Render Console MVP

## Estado

- MVP end-to-end validado.
- Checkpoint estable de UI:
  `mvp-render-console-full-flow-copy-2026-07-08`
- Checkpoint E2E PASS:
  `mvp-render-console-e2e-pass-2026-07-08`
- Rama estable:
  `custom/mvp`

## Flujo completo del MVP

```text
Crear video
-> Validar
-> Enviar a cola
-> Ejecutar runner controlado
-> API Docker
-> Render
-> MP4
-> Resultados
-> Tu video más reciente
-> Preview
-> Descargar MP4
```

## Arquitectura

* Kurukin Asset Hub materializa assets y genera `renderer-manifest.json`.
* MoneyPrinterTurbo / Render Console consume el manifest local read-only.
* MoneyPrinterTurbo no llama Asset Hub API en este MVP.
* MoneyPrinterTurbo no toca DB de Asset Hub.
* MoneyPrinterTurbo no usa rclone.
* Render Console crea jobs en cola local.
* Nightly Runner consume la cola local.
* API Docker procesa el render.
* Resultados lee MP4 generados bajo `storage/tasks`.

## Rutas importantes

Repo:

```text
/opt/moneyprinterturbo
```

WebUI privado:

```text
http://127.0.0.1:18501/Kurukin_Render_Console
```

API desde host:

```text
http://127.0.0.1:18080/api/v1
```

API desde Docker network:

```text
http://api:8080/api/v1
```

Queue dir dentro del contenedor:

```text
/MoneyPrinterTurbo/storage/nightly_jobs
```

Outputs:

```text
storage/tasks
```

Manifest de prueba validado:

```text
/data/job-assets/jab_b28367fb22d44a40bae507c175f464c4/manifests/renderer-manifest.json
```

Bundle validado:

```text
jab_b28367fb22d44a40bae507c175f464c4
```

## Comando seguro del runner controlado

La UI controlada debe calcular este comando:

```bash
python3 scripts/nightly_runner.py --max-jobs 1 --ignore-window --queue-dir /MoneyPrinterTurbo/storage/nightly_jobs --api-base-url http://api:8080/api/v1
```

Propiedades:

* `--max-jobs 1`: procesa un solo job.
* `--ignore-window`: permite ejecución manual fuera de ventana nocturna.
* `--queue-dir /MoneyPrinterTurbo/storage/nightly_jobs`: lee la misma cola que crea la UI dentro del contenedor.
* `--api-base-url http://api:8080/api/v1`: llama al API por Docker network.
* No usa `shell=True`.
* No acepta comandos arbitrarios desde UI.

## Feature flag de ejecución

La ejecución desde UI está apagada por defecto:

```text
KURUKIN_ENABLE_UI_RUNNER=<unset>
```

Para activar temporalmente:

```bash
cat >/tmp/kurukin-ui-runner-override.yml <<'YAML'
services:
  webui:
    environment:
      KURUKIN_ENABLE_UI_RUNNER: "1"
YAML

docker compose -f docker-compose.local.yml -f /tmp/kurukin-ui-runner-override.yml up -d webui
```

Para apagar después de la prueba:

```bash
docker compose -f docker-compose.local.yml up -d webui --force-recreate
rm -f /tmp/kurukin-ui-runner-override.yml
```

Validar que quedó apagado:

```bash
docker exec moneyprinterturbo-webui bash -lc 'echo "KURUKIN_ENABLE_UI_RUNNER=${KURUKIN_ENABLE_UI_RUNNER:-<unset>}"'
```

## Demo manual segura

1. Abrir Render Console.
2. Ir a `Crear video`.
3. Seleccionar `Asset Hub Bundle`.
4. Usar bundle:

```text
jab_b28367fb22d44a40bae507c175f464c4
```

5. Elegir calidad `draft_720p`.
6. Elegir subtítulos `none`.
7. Activar image motion si se desea.
8. Presionar `Validar video`.
9. Confirmar que no se crea pending todavía.
10. Presionar `Enviar a cola`.
11. Ir a `Cola` y confirmar pending.
12. Activar `KURUKIN_ENABLE_UI_RUNNER=1` temporalmente.
13. Ir a `Ejecutar`.
14. Confirmar:

    * runner disponible
    * pending count mayor a cero
    * comando seguro correcto
    * API Docker `http://api:8080/api/v1`
15. Completar confirmaciones.
16. Presionar una sola vez `Ejecutar runner controlado`.
17. Apagar flag inmediatamente después.
18. Ir a `Resultados`.
19. Confirmar `Tu video más reciente`.
20. Ver preview.
21. Descargar MP4.

## E2E PASS validado

Job:

```text
render-console-e2e-smoke-005
```

Task:

```text
6cd01cc1-48f4-4fef-8c17-f385fe9d1a54
```

MP4 final:

```text
storage/tasks/6cd01cc1-48f4-4fef-8c17-f385fe9d1a54/final-1.mp4
```

MP4 combinado:

```text
storage/tasks/6cd01cc1-48f4-4fef-8c17-f385fe9d1a54/combined-1.mp4
```

Completed dir:

```text
storage/nightly_jobs/completed/20260708-013214-render-console-e2e-smoke-005-20260708T013331Z-49-1783474411320369928
```

Resultado runner:

```text
submitted task_id=6cd01cc1-48f4-4fef-8c17-f385fe9d1a54
state=1
progress=100
moved to completed
finished jobs_started=1
```

## Guardrails

No hacer:

* No tocar `main`.
* No tocar `config.toml`.
* No tocar `resource/fonts`.
* No borrar `storage`.
* No borrar outputs MP4.
* No borrar `completed`, `failed` o `processing` sin autorización.
* No ejecutar múltiples runners.
* No repetir clicks en `Ejecutar runner controlado`.
* No hacer cleanup global.
* No llamar Asset Hub API.
* No usar rclone.
* No tocar DB.
* No tocar credenciales.
* No stagear `storage`.

## Troubleshooting

### Runner no visible dentro de WebUI

Síntoma:

```text
scripts/nightly_runner.py missing
```

Causa probable:
`./scripts` no está montado en WebUI.

Fix esperado:
`docker-compose.local.yml` monta scripts read-only:

```text
./scripts:/MoneyPrinterTurbo/scripts:ro
```

### Runner no procesa fuera de ventana nocturna

Síntoma:

```text
outside nightly window; no new jobs will be started
jobs_started=0
```

Fix:
El modo manual controlado usa:

```text
--ignore-window
```

### Runner no encuentra pending

Síntoma:

```text
no pending jobs
```

Causa probable:
Runner mirando cola distinta.

Fix:
La UI controlada debe pasar:

```text
--queue-dir /MoneyPrinterTurbo/storage/nightly_jobs
```

### Runner no puede llamar API

Síntoma:

```text
POST http://127.0.0.1:18080/api/v1/videos failed: Connection refused
```

Causa:
Dentro de WebUI, `127.0.0.1` apunta al contenedor WebUI, no al API.

Fix:
La UI controlada debe pasar:

```text
--api-base-url http://api:8080/api/v1
```

### Resultados no muestran MP4

Revisar:

```bash
docker exec moneyprinterturbo-webui bash -lc '
find storage/tasks -type f -name "*.mp4" -print -exec ls -lh {} \;
find storage/nightly_jobs/completed -maxdepth 5 -type f | sort | tail -80
'
```

También revisar si existe `final-task.json` y si contiene `videos`.

## Checks seguros

Sin ejecutar runner/render:

```bash
python3 -m py_compile app/custom/kurukin_job_queue.py app/custom/kurukin_render_console.py webui/pages/Kurukin_Render_Console.py scripts/nightly_runner.py scripts/local_job_wrapper.py

python3 -m unittest tests.custom.test_kurukin_render_console tests.custom.test_kurukin_job_queue tests.custom.test_local_job_wrapper

git diff --check

docker compose -f docker-compose.local.yml config --quiet
```

## Estado esperado en reposo

```text
KURUKIN_ENABLE_UI_RUNNER=<unset>
pending vacío
runner button disabled
MP4 existentes preservados
storage sin crecimiento inesperado
```

## Próximas features sugeridas

* Talking Head Vertical + B-roll.
* Status de cola más humano por job.
* Historial por job.
* Export/share URL.
* Limpieza segura/manual de outputs antiguos.
* Thumbnail preview.
* Filtros por fecha/task/job en Resultados.
