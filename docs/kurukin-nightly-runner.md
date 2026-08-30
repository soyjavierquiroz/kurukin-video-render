# Kurukin Nightly Runner

Kurukin Nightly Runner ejecuta jobs nocturnos para MoneyPrinterTurbo sin tocar el
core del proyecto. Usa una cola basada en archivos y envia cada job a la API
local `http://127.0.0.1:18080/api/v1`.

## Carpetas de la cola

La cola vive por defecto en:

```text
storage/nightly_jobs/
  pending/
  processing/
  completed/
  failed/
  logs/
```

- `pending/`: jobs JSON listos para ejecutar. El runner toma un job a la vez,
  ordenado por nombre de archivo.
- `processing/`: cada job activo se mueve a una carpeta unica. Dentro se guardan
  `job.json`, `moneyprinter-payload.json`, `submit-response.json` y, si aplica,
  `final-task.json` o `error.json`.
- `completed/`: jobs terminados correctamente.
- `failed/`: jobs que fallaron por validacion, excepcion, timeout o estado de
  error de MoneyPrinterTurbo.
- `logs/`: logs por sesion del runner.

El runner tambien crea `storage/nightly_jobs/nightly_runner.lock` para evitar
dos procesos simultaneos.

## Dry run

El dry run valida el job, construye el payload y lo mueve a `completed/` sin
llamar a MoneyPrinterTurbo:

```bash
python3 scripts/nightly_runner.py --dry-run --ignore-window --max-jobs 1
```

Esto crea `moneyprinter-payload.json`, una `submit-response.json` simulada y
`final-task.json` con `state: 1`.

## Run real manual

Para ejecutar manualmente ignorando la ventana nocturna:

```bash
python3 scripts/nightly_runner.py --ignore-window
```

Para usar las reglas normales de horario:

```bash
python3 scripts/nightly_runner.py
```

Defaults principales:

```text
--queue-dir /MoneyPrinterTurbo/storage/nightly_jobs si existe; si no, <project_root>/storage/nightly_jobs
--api-base-url http://127.0.0.1:18080/api/v1
--window-start 00:00
--window-end 07:00
--max-jobs 10
--poll-seconds 20
--task-timeout-seconds 14400
--no-progress-timeout-seconds 1800
```

En contenedor, el default portable usa `/MoneyPrinterTurbo/storage/nightly_jobs`
cuando esa ruta existe. En host o repo local, si esa ruta no existe, el default
cae en `<project_root>/storage/nightly_jobs`.

La UI controlada no depende del default: siempre pasa la cola explicita del
contenedor al ejecutar manualmente un solo job:

```bash
python3 scripts/nightly_runner.py --max-jobs 1 --ignore-window --queue-dir /MoneyPrinterTurbo/storage/nightly_jobs
```

`--task-timeout-seconds` es el timeout global maximo del task completo. Aunque
el task siga respondiendo, el runner no esperara mas que ese limite total.

## Cron a medianoche

Ejemplo para iniciar el runner todos los dias a medianoche:

```cron
0 0 * * * cd /opt/moneyprinterturbo && /usr/bin/python3 scripts/nightly_runner.py >> storage/nightly_jobs/logs/cron.log 2>&1
```

## Estados de MoneyPrinterTurbo

El runner interpreta los estados de la API asi:

```text
state = 1   complete
state = -1  failed
state = 4   processing
```

Mientras el estado sea `4`, el runner consulta
`GET /api/v1/tasks/{task_id}` cada `--poll-seconds`.

Ademas, el runner observa el ultimo par `state`/`progress` reportado por
`GET /api/v1/tasks/{task_id}`. Si ninguno de los dos cambia durante mas de
`--no-progress-timeout-seconds` segundos, el job falla y se mueve a `failed/`
con `error.json`. El default es `1800` segundos.

Este timeout protege contra tasks internos congelados en `state=4`, por ejemplo
cuando MoneyPrinterTurbo queda pegado tras una excepcion interna como una
configuracion incompleta de `pexels_api_keys`.

## Regla de ventana nocturna

Por defecto solo se inician jobs nuevos entre `00:00` y `07:00`. Si un job ya
esta corriendo despues de las `07:00`, el runner lo deja terminar. Al volver al
loop, si ya esta fuera de ventana, no inicia el siguiente job.

Use `--ignore-window` para pruebas o ejecuciones manuales fuera de horario.
