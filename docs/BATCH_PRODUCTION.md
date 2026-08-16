# Batch Production

`scripts/produce_batch.py` permite producir un lote completo desde una carpeta
con audios y guiones ya preparados.

## Carpeta De Input

Crea una carpeta bajo `storage/batch_inputs/`:

```text
storage/batch_inputs/lote-001/
  abandono.mp3
  abandono.txt
  ansiedad.mp3
  ansiedad.txt
  pareja.mp3
  pareja.txt
```

El nombre de la carpeta se usa como `batch_id`. En el ejemplo:

```text
lote-001
```

## Naming

Cada video se empareja por stem exacto:

```text
abandono.mp3
abandono.txt
```

Ese job se llama `abandono`.

También son válidos stems seguros como:

```text
01.mp3
01.txt
video-ansiedad.mp3
video-ansiedad.txt
```

No se empareja por posición ni por orden alfabético. Si existe `foo.mp3`,
debe existir `foo.txt`, o el batch aborta antes de empezar.

## SRT Opcional

Puedes añadir un SRT custom con el mismo stem:

```text
abandono.mp3
abandono.txt
abandono.srt
```

Cuando existe, ese SRT gana. El runner no ejecuta Whisper para ese job y pasa
directamente a HyperFrames.

## Dry Run

Antes de producir, valida la carpeta:

```bash
cd /opt/moneyprinterturbo

python3 scripts/produce_batch.py \
  storage/batch_inputs/lote-001 \
  --dry-run
```

El dry-run muestra los pares encontrados, los task IDs deterministas y si cada
job tiene custom SRT. No ejecuta MPT, Whisper ni HyperFrames.

## Producción

Ejecuta:

```bash
cd /opt/moneyprinterturbo

python3 scripts/produce_batch.py \
  storage/batch_inputs/lote-001
```

Defaults v1:

```text
preset HyperFrames: editorial-gold
position: bottom
aspect: 9:16
concat: sequential
BGM: off
audio: MP3 suministrado
script: TXT suministrado
subtitles MPT: off
source mode: generic multi-provider
```

También puedes cambiar lo mínimo:

```bash
python3 scripts/produce_batch.py \
  storage/batch_inputs/lote-001 \
  --preset editorial-gold \
  --position bottom
```

## Outputs

Cada task conserva su master:

```text
storage/tasks/<task_id>/final-1.mp4
```

El subtitulado final queda en:

```text
storage/tasks/<task_id>/final-subtitled.mp4
```

El output cómodo del lote queda nombrado por stem:

```text
storage/batch_outputs/lote-001/abandono.mp4
storage/batch_outputs/lote-001/ansiedad.mp4
storage/batch_outputs/lote-001/pareja.mp4
```

El reporte está en:

```text
storage/batch_outputs/lote-001/batch-report.json
```

Logs detallados por job:

```text
storage/batch_outputs/lote-001/logs/
```

## Reanudar

Puedes ejecutar el mismo comando otra vez.

El runner salta etapas ya completadas:

```text
final-1.mp4 existe -> salta MASTER
subtitle.srt + subtitle-alignment.json aprobado -> salta SUBTITLES
final-subtitled.mp4 existe -> salta HYPERFRAMES
```

No borra outputs anteriores automáticamente y no sobrescribe `final-1.mp4`.

## REVIEW_REQUIRED

Si Whisper + alignment no alcanza confianza suficiente, el job queda como:

```text
review_required
```

En ese caso no se ejecuta HyperFrames para ese video. El resto del batch sigue.

Revisa:

```text
storage/tasks/<task_id>/subtitle.srt
storage/tasks/<task_id>/subtitle.raw.srt
storage/tasks/<task_id>/subtitle-alignment.json
```

Después de corregir el SRT o aprobarlo, vuelve a ejecutar el mismo comando para
continuar.
