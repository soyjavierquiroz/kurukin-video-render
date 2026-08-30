# Auditoría upstream MoneyPrinterTurbo v1.3.2

Fecha de auditoría: 2026-07-20

## Alcance y restricciones

Esta auditoría compara la rama estable de Kurukin con MoneyPrinterTurbo upstream sin hacer merge, rebase, upgrade, deploy, render ni llamadas a proveedores. No se modificaron `main`, `config.toml`, `resource/fonts` ni el código de la aplicación. El único cambio de trabajo previsto es este documento.

## Estado local observado

- Repositorio: `/opt/moneyprinterturbo`.
- Rama activa: `custom/mvp`.
- `HEAD` observado al iniciar: `d4048aa9884dde68d147cccfbda9d9e75d40100e` (`docs: record mpt stock render smoke pass`).
- Checkpoint indicado para la auditoría: `f89d738` (`mvp-manual-queue-process-v1-pass-2026-07-12`).
- Discrepancia: `custom/mvp` contiene commits posteriores al checkpoint indicado; `f89d738` sigue presente en su historia, pero no es el `HEAD` actual.
- Tag local indicado y confirmado en el historial: `mvp-manual-queue-process-v1-pass-2026-07-12`.
- Estado inicial del árbol: limpio.
- `origin`: `git@github.com:soyjavierquiroz/kurukin-video-render.git`.

## Upstream y versión detectada

- Remote usado: `upstream`, fetch desde `https://github.com/harry0703/MoneyPrinterTurbo.git`.
- El push de `upstream` está deshabilitado localmente (`DISABLED`).
- Se ejecutó únicamente `git fetch upstream --tags --prune`.
- Tag de versión más alto encontrado: `v1.3.2`.
- `v1.3.2` existe localmente y resuelve a `b1588e1fdc6c5e54358f66ca2ff323e1dddf1364`.
- `upstream/main` resuelve a `d5f31da9a2eb63c96addd66e17e5b8042ed010d4`.
- `upstream/main` está 20 commits por delante de `v1.3.2`; no se encontró un tag `v*` superior a `v1.3.2`. Por tanto, `v1.3.2` es el release etiquetado más reciente, pero no contiene los cambios más recientes de `main`.

## Distancia entre `custom/mvp` y `v1.3.2`

- `custom/mvp`: `d4048aa` al auditar.
- Ancestro común: `48b08719c9690739a79dc70665db8dbd109c2afc`.
- Divergencia desde el ancestro común (`git rev-list --left-right --count custom/mvp...v1.3.2`): 166 commits exclusivos de `custom/mvp` y 9 commits exclusivos de `v1.3.2`.
- Comparación directa `custom/mvp..v1.3.2`: 164 archivos, aproximadamente 10,483 inserciones y 36,247 eliminaciones.
- Cambios por lado desde el ancestro común: 110 rutas en `custom/mvp`, 59 rutas en `v1.3.2` y 5 rutas modificadas por ambos lados.

La cifra alta de eliminaciones de la comparación directa no significa que upstream haya borrado activamente Kurukin: muchos archivos `app/custom/*`, pruebas y documentos existen sólo en `custom/mvp` y, por definición, no están en el tag upstream. Un merge directo, sin embargo, requeriría preservar explícitamente esa capa propia.

Los nueve commits exclusivos del tag incluyen: copy/configuración de proveedores LLM, idioma español para guion, publicación de v1.3.1, documentación, centralización de defaults/tips de proveedores, duración de audio custom no-MP3, margen de seguridad de duración de vídeo y la modernización amplia de WebUI/generación que publica v1.3.2.

## Archivos sensibles

### Tocadas por upstream entre el ancestro común y `v1.3.2`

- `app/models/schema.py`
- `app/services/voice.py`
- `app/services/video.py`
- `webui/Main.py`, `webui/styles.css`, `webui/.streamlit/config.toml` y todos los JSON de i18n bajo `webui/i18n/`
- `requirements.txt`
- `pyproject.toml`
- `uv.lock`
- `Dockerfile` y `Dockerfile.gpu`
- `config.example.toml`
- `app/config/config.py`
- Varios `docker-compose*.yml` (`docker-compose.yml`, GPU y release)

### No tocadas por upstream en esos nueve commits

- `webui/pages/Kurukin_Render_Console.py` (existe sólo/cambia del lado custom)
- `app/custom/*` (existe sólo/cambia del lado custom)
- `app/services/task.py` (cambia del lado custom, no del lado upstream en este rango)
- `app/services/material.py`
- `app/services/subtitle.py`
- `docker-compose.local.yml` (cambia del lado custom)
- `config.toml`

### Solapamiento real de cambios de ambos lados

Las cinco rutas modificadas tanto por `custom/mvp` como por `v1.3.2` desde el ancestro común son:

- `.gitignore`
- `app/config/config.py`
- `app/models/schema.py`
- `app/services/video.py`
- `webui/Main.py`

Éstas concentran el mayor riesgo de conflicto semántico. Aunque `app/custom/*` y `Kurukin_Render_Console.py` no sean solapamientos Git directos, dependen de contratos de `schema.py`, `video.py`, configuración y WebUI; necesitan pruebas de integración antes de adoptar upstream.

## Features útiles para producción Ruta B

### Confirmadas en `v1.3.2`

- Flujo guion/materiales: `match_materials_to_script` está expuesto en schema, controlador LLM, servicio de tareas y WebUI. Fuerza orden secuencial y aumenta la cantidad de términos/materiales solicitados para alinear clips con el guion.
- Stock: soporte de Pexels, Pixabay y Coverr en `app/services/material.py` y selección/configuración en WebUI. Coverr añade una tercera fuente, con advertencia de que su catálogo es mayormente horizontal.
- Audio custom: el fix `18d577f` obtiene duración mediante FFmpeg/MoviePy para formatos soportados como MP3, M4A, WAV y AAC, en vez de limitarse a rutas `.mp3`. Es directamente relevante para la entrada audio de Ruta B.
- Duración/concat: el fix `8a6fcb7` añade margen de seguridad y límite de duración al concat FFmpeg. Reduce finales cortados o discrepancias frente al audio.
- Subtítulos: generación Whisper disponible para audio custom; endpoint de sólo subtítulo; corrección/alineación contra guion; controles de fondo/redondeado; validación de color texto/fondo y cobertura de glifos de fuente; el fondo queda desactivado por defecto en schema.
- TTS: ElevenLabs está integrado, además de Azure, Edge, Gemini y otros proveedores. v1.3.2 también mejora rate/SSML de Azure y actualiza el cliente de Gemini. Su uso real requiere validación aislada y credenciales; esta auditoría no llamó a ninguno.
- LLM/proveedores: registro y defaults centralizados, más copy/tips de proveedores y manejo de configuración renovado.
- Operación: `cli.py` fue reestructurado ampliamente; v1.3.2 incorpora flujos modernos de WebUI, historial/cache de tareas y un skill/agent auxiliar. Esto puede ayudar a operación headless, pero no reemplaza automáticamente la cola y contratos propios de Kurukin.
- Español: opción explícita de español como idioma del guion.

### Cambios útiles que están sólo después del tag

Los 20 commits actuales de `upstream/main` incluyen, entre otros, endurecimiento y recuperación de tareas, prevención de fallback Whisper inesperado, corte de generación de términos ante errores de proveedor, tolerancia de materiales apenas por debajo de resolución mínima, preview completo de voz y correcciones de limpieza de temporales. No deben atribuirse a `v1.3.2` ni incorporarse sin una auditoría separada.

## Riesgos de actualizar

1. `webui/Main.py` tiene un cambio upstream masivo y también cambios custom. Es el área de mayor riesgo para la consola, estados de sesión y cola manual de Kurukin.
2. `app/models/schema.py` y `app/services/video.py` cambiaron en ambos lados. Hay riesgo sobre contratos de jobs, parámetros de subtítulos, duración y concat.
3. `app/config/config.py` cambió en ambos lados; upstream v1.3.2 amplía y sincroniza configuración. Debe evitarse cualquier escritura accidental sobre `config.toml` durante pruebas.
4. Upstream cambia `requirements.txt`, `pyproject.toml` y `uv.lock`; una adopción obliga a validar compatibilidad de dependencias en un entorno aislado, sin instalar en producción.
5. Los Dockerfiles y compose upstream cambiaron. `docker-compose.local.yml` es propio/diferente y no debe reemplazarse mecánicamente.
6. La capa `app/custom/*` y `Kurukin_Render_Console.py` no existen upstream. Una comparación directa los muestra como eliminados; una integración mal planteada podría perder Ruta B, cola, adapter y guardas propias.
7. El nuevo WebUI maneja settings y credenciales de forma distinta. Se requiere revisar que ninguna acción de UI persista configuración o dispare proveedores durante smoke tests.
8. El tag está 20 commits detrás de `upstream/main`. Integrar hoy v1.3.2 puede dejar fuera fixes operativos valiosos, pero perseguir `main` aumentaría alcance e inestabilidad.

## Recomendación

**Recomendación principal: A, no actualizar todavía y activar producción Ruta B sobre el `custom/mvp` actualmente validado.** La divergencia es grande, el núcleo custom es sustancial y los beneficios de v1.3.2 no justifican arriesgar el checkpoint productivo antes de reactivar y observar la ruta existente.

En paralelo y sin bloquear producción, aplicar **B: crear más adelante una rama de integración dedicada desde `custom/mvp` para evaluar `v1.3.2`**. No debe hacerse sobre `custom/mvp` ni sobre `main`; debe preservar la capa custom y contar con una matriz de pruebas offline.

Usar **C, cherry-pick selectivo**, sólo para fixes pequeños, autocontenidos y demostrablemente compatibles. Los primeros candidatos para investigar son `18d577f` (duración de audio custom no-MP3) y `8a6fcb7` (margen de seguridad de duración). No se recomienda cherry-pick ciego de `b1588e1`, porque su modernización WebUI es amplia y mezcla UI, configuración, dependencias, voz, vídeo y CLI.

## Próximos pasos de menor fricción

1. Confirmar formalmente que el checkpoint productivo deseado es `d4048aa` y no el anterior `f89d738`; no mover ninguna rama hasta resolver esa referencia documental.
2. Reactivar Ruta B en el `custom/mvp` ya validado, usando el runbook existente y guardas de no-proveedor adecuadas al despliegue.
3. Registrar métricas productivas de cola, duración audio/vídeo, calidad de subtítulos y relevancia visual antes de cambiar el core.
4. Crear, en una ventana posterior y con autorización explícita, una rama tipo `integration/mpt-v1.3.2` desde el checkpoint confirmado.
5. Resolver primero las cinco rutas de solapamiento, empezando por schema/config/video y dejando `webui/Main.py` para una revisión dedicada.
6. Ejecutar en esa rama pruebas offline de contratos de jobs, cola/manual process, audio custom, subtítulos, concat/duración y fuentes locales; mantener proveedores remotos desactivados.
7. Evaluar por separado los dos fixes selectivos `18d577f` y `8a6fcb7` con pruebas unitarias y regresión de Ruta B.
8. Auditar los 20 commits posteriores a v1.3.2 antes de decidir si conviene integrar el tag o esperar al siguiente release etiquetado.

## Confirmación de seguridad de esta auditoría

Durante la auditoría no se hizo merge, rebase, deploy, push, render ni ejecución de runner o endpoints de vídeo. No se llamó a OpenAI, TTS, Pexels, Pixabay, Coverr, Asset Hub ni otros proveedores. No se modificaron `config.toml`, `resource/fonts`, storage ni código de la aplicación, y no se instalaron dependencias.
