# Kurukin Source Provider Env Wiring

## Objetivo

Permitir que `docker-compose.local.yml` pase aliases de variables de entorno de
proveedores al proceso `webui` sin hardcodear ni commitear secretos.

## Passthrough actual

- `PEXELS_API_KEY`
- `PEXELS_KEY`
- `PIXABAY_API_KEY`
- `PIXABAY_KEY`
- `COVERR_API_KEY`
- `COVERR_KEY`
- `KURUKIN_ENABLE_PEXELS_SOURCE`

## Guardrails

- Antes de crear `.env`, confirmar `git check-ignore -v .env`.
- `.env.example` contiene solo placeholders vacios.
- La verificacion dentro del contenedor debe reportar booleanos, no valores.
- No usar `docker compose config` sin `--quiet` cuando existan secretos
  cargados.
- Este wiring no contacta proveedores ni dispara renders por si mismo.
