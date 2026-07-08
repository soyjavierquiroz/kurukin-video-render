# Kurukin A-roll / B-roll: multiple B-roll assets

## Estado

Soporte MVP preparado para usar multiples assets B-roll locales en el layout
estable `alternating_fullscreen`, sin cambiar el contrato de audio o duracion.

## Limites

- Se requiere al menos 1 asset y se aceptan como maximo 8.
- Los paths deben permanecer bajo los roots locales permitidos.
- Las lineas vacias se ignoran.
- Los paths duplicados exactos se eliminan preservando el primer orden.

## Rotacion

Cada nuevo segmento visual B-roll usa el siguiente asset de la lista. Despues
del ultimo asset, el timeline vuelve al primero.

Ejemplo: con assets `1, 2, 3`, los segmentos B-roll usan `1, 2, 3, 1`.

## Guardrails

- El A-roll manda el audio y la duracion final.
- El audio B-roll queda muted porque no se mapea.
- El timeline no crea segmentos de duracion cero o negativa.
- El comando se mantiene como `list[str]`, sin `shell=True`.
- El dry-run no crea task dir, pending ni MP4.
- No hay seleccion semantica, matching de transcript ni layouts nuevos.
- No se llama Asset Hub API.

## Siguiente fase

Smoke real multi B-roll con fixtures pequenos, solo despues de autorizacion
explicita para ejecutar runner o ffmpeg.
