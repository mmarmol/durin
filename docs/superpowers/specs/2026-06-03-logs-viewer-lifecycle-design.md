# Logs: ciclo de vida + visor web — diseño

Fecha: 2026-06-03
Branch: `worktree-logs-viewer-lifecycle` (desde `main` @ 6c3cc24)

## 1. Problema y alcance

El dashboard web no tiene forma de ver los logs. Pero antes de construir un visor
hay que arreglar el sustrato: hoy las dos fuentes de logs **solo se acumulan** y
una de ellas **pierde datos en silencio**. Construir el visor encima sin resolver
esto es fabricar deuda.

Tres problemas verificados en código:

1. **`gateway.log` crece sin límite.** Archivo único en append, sin rotación ni
   purga ([`durin/cli/gateway_daemon.py:58-61`](../../../durin/cli/gateway_daemon.py),
   [`:152-162`](../../../durin/cli/gateway_daemon.py)). Solo se reduce borrándolo a mano.
2. **La telemetría se acumula para siempre.** Rota por día/sesión en el nombre de
   archivo, pero los archivos viejos no se borran nunca
   ([`durin/telemetry/logger.py:144-160`](../../../durin/telemetry/logger.py)).
3. **La telemetría pierde datos en silencio.** A los 10.000 eventos por archivo,
   `log()` hace `return` y descarta sin avisar
   ([`durin/telemetry/logger.py:91-92`](../../../durin/telemetry/logger.py)).

El alcance de este trabajo es, por lo tanto, **dos partes**:

- **Parte A — Ciclo de vida de logs** (prerrequisito): rotación por tamaño,
  retención por días, sin compresión, configurable desde la web.
- **Parte B — Visor web**: nueva sección "Logs" en Configuraciones, con dos tabs
  (Gateway / Telemetría), filtros, orden por fecha y search.

**Fuera de alcance:** redacción de secretos en logs, streaming en vivo (tail -f por
websocket), correlación temporal sincronizada entre las dos fuentes (posible v2).

## 2. Decisiones tomadas (con su razón)

| Decisión | Valor | Razón |
|---|---|---|
| Combinar fuentes | Dos tabs en una misma sección | Las fuentes solo comparten timestamp; un stream único aplanaría la estructura de la telemetría y rompería el filtro por categoría. |
| Frescura | Snapshot + refresh manual | Es un visor/explorador, no un monitor en vivo. Endpoint REST simple. |
| Alcance telemetría | Global cronológico, columna `session` | Vista "todo lo que pasó"; deja la puerta abierta a un *action* por sesión a futuro. |
| Rotación | **Por tamaño, 5 MB/archivo** | Unidad "procesable" por la web; ~10-30k eventos JSONL o ~50k líneas de texto, escaneo sub-segundo. Valor probado en hermes-agent. |
| Al llegar al límite | **Saltar a archivo nuevo** (no descartar) | Elimina la pérdida silenciosa de datos (#3). |
| Retención | **Por antigüedad, default 3 días** | Configurable desde la web. |
| Compresión | **No** | Con 3 días de retención el ahorro es marginal y comprimir vuelve los archivos no-grepeables / obliga a descomprimir al vuelo. |

## 3. Parte A — Ciclo de vida de logs

### 3.1 Configuración

Nueva sección `logging:` en el modelo de config Pydantic (`DurinConfig`). Dos
perillas, ambas editables desde la web vía el `/api/config/set` que ya existe:

| Clave | Default | Qué hace |
|---|---|---|
| `logging.max_file_mb` | `5` | Al superar este tamaño, la fuente salta al archivo siguiente. |
| `logging.retention_days` | `3` | Se borran los archivos con más de N días. |

Aplica a las **dos** fuentes (una sola perilla de tamaño y una de retención, menos
config que mantener).

### 3.2 Gateway log

Migrar de la redirección cruda de stdout/stderr a un **sink de loguru propio del
daemon**, que soporta rotación + retención nativas:

```
logger.add(daemon_logs_path(), rotation="<max_file_mb> MB", retention="<retention_days> days")
```

- Rotación produce `gateway.log.1`, `gateway.log.2`, … (sin compresión).
- **Wrinkle a resolver en el plan:** hoy el padre redirige stdout/stderr del
  subproceso al archivo. Al pasar a un sink loguru que es dueño del archivo, hay que
  capturar lo que hoy llega por stdout/stderr crudo (tracebacks de excepciones no
  capturadas, prints de librerías):
  - El `logging_bridge` ([`durin/utils/logging_bridge.py`](../../../durin/utils/logging_bridge.py))
    ya enruta `logging` de stdlib → loguru.
  - Agregar un `sys.excepthook` que enrute tracebacks no capturados a loguru.
  - Prints crudos a stdout que no pasen por logging son pérdida aceptable (o se
    redirige stdout/stderr del daemon a loguru con un wrapper; decisión del plan).

### 3.3 Telemetría

- **Roll por tamaño:** antes de escribir, si el archivo activo supera `max_file_mb`,
  abrir la parte siguiente `{sesión}_{fecha}_p2.jsonl`, `_p3`, … y seguir.
- **Eliminar el cap de 10.000 eventos** ([`logger.py:91-92`](../../../durin/telemetry/logger.py))
  — lo reemplaza el límite por tamaño. Cero pérdida silenciosa.
- Un archivo rola por **tamaño** (`_pN`) **o** por **cambio de día** (la fecha ya
  está en el nombre vía `get_session_logger`).
- **Retención por antigüedad:** borrar archivos `*.jsonl` con `mtime` > `retention_days`.

### 3.4 Disparo de la purga

- Una pasada de limpieza al **iniciar el daemon** (barata, acotada) — patrón de openclaw.
- **+ un job en el cron** que durin ya tiene, para que un daemon de larga vida también
  purgue sin reiniciar.

La purga es común a las dos fuentes: borra por `mtime > retention_days` en
`~/.durin/logs/` (gateway + backups) y `~/.cache/durin/telemetry/` (JSONL + partes).

## 4. Parte B — Visor web

### 4.1 Estructura UI

- Nueva entrada `logs` en la barra lateral de
  [`webui/src/components/settings/SettingsView.tsx`](../../../webui/src/components/settings/SettingsView.tsx)
  (ícono `ScrollText`), siguiendo el patrón de `CronSettings.tsx`:
  - Agregar `"logs"` al tipo `SettingsSectionKey`.
  - Agregar el ítem a `SETTINGS_NAV_ITEMS`.
  - Agregar el caso de dispatch en el render principal.
- Nuevo componente `webui/src/components/settings/LogsSettings.tsx` con:
  - Selector de tab arriba: **Gateway** | **Telemetría**.
  - Barra de filtros sticky + botón **Refrescar**.
  - Tabla densa, monoespaciada, scrolleable. Orden por fecha **descendente** por
    defecto, con toggle asc/desc.
  - Header con las dos perillas de config (`max_file_mb`, `retention_days`) editables.

### 4.2 Tab Gateway

- Fuente: `gateway.log` + backups rotados.
- Columnas: **fecha · nivel · canal · mensaje**.
- Filtros: **nivel** (multi: DEBUG/INFO/WARN/ERROR…), **canal** (dropdown),
  **rango de fecha**, **search** (substring sobre la línea completa).
- Parsing server-side del formato fijo de loguru (`fecha | nivel | canal | mensaje`).
  Líneas que no matchean (stack traces multilínea) se adjuntan a la línea anterior
  como continuación.

### 4.3 Tab Telemetría

- Fuente: todos los `~/.cache/durin/telemetry/*.jsonl` (incluyendo partes `_pN`),
  mezclados y ordenados por `ts`.
- Columnas: **fecha · sesión · tipo · resumen del `data`**.
- Filtros: **sesión** (dropdown), **tipo de evento** (multi, agrupado por familia:
  `memory.*`, `tool_*`, `provider.*`…), **rango de fecha**, **search** (substring
  sobre el JSON serializado del evento, matchea cualquier campo del `data`).
- Click en una fila la expande para ver el `data` completo en JSON.
- La columna `session` queda preparada para un *action* futuro (ir a ese momento de
  la sesión / abrir la sesión en la sección de Memoria) — no se implementa ahora.

### 4.4 API

Un único endpoint REST nuevo en
[`durin/channels/websocket.py`](../../../durin/channels/websocket.py), agregado al
dispatch de `_dispatch_http()`, siguiendo el patrón de `_handle_config_get`
(token Bearer vía `_check_api_token` + `_http_json_response`):

```
GET /api/logs?source=gateway|telemetry
              &level=&channel=&session=&type=    (filtros según source)
              &q=<search>&since=&until=
              &limit=200&offset=0&order=desc
```

Respuesta:

```json
{
  "lines": [ { ... } ],
  "total": <int>,
  "facets": { "levels": [...], "channels": [...], "sessions": [...], "types": [...] }
}
```

- **Filtrado y paginado del lado del server.**
- `facets` puebla los dropdowns de filtro sin escanear todo en el cliente.
- **Robusto a archivos grandes:** leer `gateway.log` desde el final / por bloques,
  nunca cargar el archivo entero en memoria. Para telemetría, escanear archivos por
  fecha; la ventana de escaneo por defecto es `retention_days` (no tiene sentido
  barrer más allá de lo que igualmente se purga).

### 4.5 Cliente

Función `fetchLogs(token, query)` en
[`webui/src/lib/api.ts`](../../../webui/src/lib/api.ts), usando el wrapper
`request<T>` existente (Bearer token + reauth en 401).

## 5. Plan de pruebas (gates)

- **Lifecycle / rotación gateway:** escribir > `max_file_mb` y verificar que se crea
  `gateway.log.1`; verificar retención borra > `retention_days` (mtime simulado).
- **Lifecycle / roll telemetría:** superar `max_file_mb` y verificar que se crea
  `_p2.jsonl` y que NO se pierde ningún evento (contar eventos in vs out).
- **Cap eliminado:** test que antes fallaba a 10k ahora escribe > 10k sin pérdida.
- **Purga:** archivo con mtime > `retention_days` se borra al iniciar daemon y en el cron.
- **Config:** `logging.max_file_mb` / `logging.retention_days` se leen del config y
  se pueden setear vía `/api/config/set`.
- **API `/api/logs`:** 401 sin token; filtros por nivel/canal/sesión/tipo; search;
  paginación; orden; `facets` correctos; lee partes `_pN` de telemetría.
- **Subprocess/CI env:** la migración del gateway a loguru debe verificarse bajo
  `HOME=/tmp/empty` + `GIT_CONFIG_NOSYSTEM=1` (regla del proyecto para tools que leen config).
- **Webui:** verificación contra el binario/webui real (no solo unit tests verdes):
  abrir la sección Logs, ambos tabs, aplicar filtros, refrescar.

## 6. Mapa de archivos a tocar

**Backend:**
- `durin/config*.py` — sección `logging:` (`max_file_mb`, `retention_days`).
- `durin/cli/gateway_daemon.py` — sink loguru con rotation/retention + excepthook.
- `durin/telemetry/logger.py` — roll por tamaño, eliminar cap 10k.
- nuevo módulo de purga (o función) + enganche en arranque del daemon + cron.
- `durin/channels/websocket.py` — handler `_handle_logs_list` + dispatch.

**Frontend:**
- `webui/src/components/settings/SettingsView.tsx` — nav + dispatch de `logs`.
- `webui/src/components/settings/LogsSettings.tsx` — nuevo, dos tabs + filtros.
- `webui/src/lib/api.ts` — `fetchLogs`.

**Docs:**
- `docs/ARCHITECTURE.md` — actualizar (toca telemetry + channels, módulos core).
