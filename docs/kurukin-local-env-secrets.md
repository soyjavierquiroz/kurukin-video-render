# Kurukin Local Env Secrets

## Objetivo

Definir una forma segura de manejar variables locales de entorno y secretos sin
commitear valores reales.

## Reglas

- `.env` local esta ignorado por git y no se commitea.
- `.env.example` es una plantilla sin secretos reales.
- Las claves de proveedores se colocan manualmente en `.env` en el servidor o
  en el entorno que ejecuta Docker Compose.
- La verificacion permitida es booleana solamente.
- No se imprimen valores en logs, docs ni salidas de comandos.
- No usar `docker compose config` sin `--quiet` si pudiera expandir valores.
- Un smoke real con proveedor requiere autorizacion explicita.

## Flujo recomendado

1. Confirmar `git check-ignore -v .env`.
2. Copiar `.env.example` a `.env` fuera de git.
3. Cargar valores reales solo en el host o servidor controlado.
4. Recrear el contenedor necesario.
5. Verificar disponibilidad con booleanos, nunca con valores.
