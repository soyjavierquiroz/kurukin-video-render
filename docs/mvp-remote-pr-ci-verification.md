# MVP Remote PR / CI Verification

Fecha: 2026-07-09

## Commit verificado

- Branch local verificada: `custom/mvp`
- Commit remoto verificado: `37ba3f9eafe624fee94a7b917717998ca5a3fb21`
- Branch remoto: `origin/custom/mvp`
- Tag remoto: `mvp-local-env-secrets-gitignore-2026-07-09`

## Validaciones remotas y de checkout

- `origin/custom/mvp` existe y apunta al commit verificado.
- El tag remoto publicado existe y apunta al mismo commit verificado.
- `git pull --ff-only origin custom/mvp`: OK
- Working tree local: limpio
- `main` no fue tocado ni mergeado.

## Secret scan

- `.env` existe localmente pero sigue ignorado por git.
- `.env.local` queda ignorado por la regla `/.env.*`.
- `.env.example` no esta ignorado y permanece versionable.
- `.env.example` contiene solo placeholders vacios, sin valores reales.
- No se imprimieron secretos ni se commitearon credenciales durante esta
  verificacion.

## Checks

- `git diff --check`: PASS
- `docker compose -f docker-compose.local.yml config --quiet`: PASS
- `curl http://127.0.0.1:18501/Kurukin_Render_Console`: PASS (`200 1522`)
- `storage/` sigue ignored y no stageado.

## Unittest

- Host: `python3 -m unittest` FAIL por dependencias ausentes del entorno local
  (`loguru`, `pydantic`, `numpy`, `moviepy`).
- Contenedor `webui`: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest` FAIL con
  2 fallos y 7 skipped.
- Fallos observados:
  - `test.services.test_voice.TestVoiceService.test_gemini_tts_uses_legacy_submaker_fields`
  - `test.services.test_voice.TestVoiceService.test_generate_subtitle_keeps_edge_provider_for_gemini_legacy_submaker`

## Estado PR-ready

- Parcial.
- El branch/tag remoto y los guardrails de secretos estan correctos.
- El estado no es CI-green para un PR general mientras el suite completo del
  contenedor mantenga esos 2 fallos.

## Riesgos pendientes

- El suite completo del contenedor no esta en verde.
- El host local no tiene todas las dependencias para ejecutar `python3 -m unittest`
  de punta a punta.
- La verificacion de secretos fue booleana/estructural; no se inspeccionaron ni
  imprimieron valores de `.env`.

## Siguiente hito recomendado

1. Resolver o aislar los 2 fallos de `test.services.test_voice` en una rama
   dedicada.
2. Repetir `python3 -m unittest` dentro del contenedor `webui` hasta obtener
   PASS completo.
3. Reabrir la verificacion remota/CI una vez que el suite total quede verde.
