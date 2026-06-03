# Logs: ciclo de vida del gateway + visor web — diseño

Fecha: 2026-06-03
Branch: `worktree-logs-viewer-lifecycle` (desde `main` @ 6c3cc24)

## 1. Alcance final

El dashboard web no tiene forma de ver los logs. Este trabajo agrega un **visor de
logs** en Configuraciones y, como prerrequisito, arregla el **ciclo de vida del log
del gateway** (hoy crece sin límite).

**Decisión de alcance clave (corregida sobre la marcha):** la **telemetría
estructurada NO se toca**. Ya tiene un ciclo de vida deliberado y funcionando
([`durin/telemetry/retention.py`](../../../durin/telemetry/retention.py): comprime a
30 días, borra a 90, enganchado al health-check tick) y la usan los agentes
auto-gestionados para tomar decisiones. El visor la **lee en modo solo-lectura**; no
modificamos su logger, su retención ni exponemos su config.

Entonces, dos partes:

- **Parte A — Ciclo de vida del log del gateway:** `gateway.log` pasa a JSONL,
  rota por tamaño, comprime los segmentos rotados, retiene por días. Configurable
  desde la web.
- **Parte B — Visor web:** sección "Logs" en Configuraciones con dos tabs
  (Gateway / Telemetría), ambos construidos sobre **un único primitivo de lectura**.

**Fuera de alcance:** tocar el backend de telemetría; redacción de secretos en logs;
streaming en vivo (tail -f por websocket); índice FTS/SQLite sobre logs; correlación
temporal sincronizada entre las dos fuentes.

## 2. Decisiones (con su razón)

| Decisión | Valor | Razón |
|---|---|---|
| Telemetría backend | **Intacto** | La usan los agentes para decidir; ya tiene ciclo de vida propio. Solo lectura en el visor. |
| Formato gateway.log | **JSONL** (loguru `serialize=True`) en el sink de archivo | Filtrado robusto sin regex ni pegado de stack-traces; **parejo con telemetría** → un solo parser. La terminal sigue en texto coloreado humano (dos sinks). |
| Rotación gateway | **Por tamaño, 5 MB** | Unidad procesable; ~10-25k líneas/segmento, escaneo ~100 ms. |
| Al llegar al límite | **Saltar a segmento nuevo** | Sin pérdida. |
| Compresión rotados | **Sí, `.gz`** (loguru `compression="gz"`) | Costo ~0: el visor ya descomprime `.gz` para telemetría, y loguru lo hace con un parámetro. El segmento **activo** queda plano (no se appendea a gzip). |
| Retención gateway | **Por días, default 7** | Con compresión el disco es barato; 7 días te salva cuando notás un problema a los días. Configurable. |
| Frescura | Snapshot + refresh manual | Es un visor/explorador, no un monitor en vivo. |
| Búsqueda/listado | Ver §4 (primitivo de lectura) | El costo escala con la página, no con el corpus. |

## 3. Parte A — Ciclo de vida del log del gateway

### 3.1 Configuración

Nueva sección `logging:` en el modelo Pydantic (`Config`), editable desde la web vía
el `/api/config/set` existente. Gobierna **solo el log del gateway**:

| Clave | Default | Qué hace |
|---|---|---|
| `logging.max_file_mb` | `5` | Tamaño al que `gateway.log` rota a un segmento nuevo. |
| `logging.retention_days` | `7` | Edad a la que se borran los segmentos rotados. |

### 3.2 Sink de archivo JSONL con rotación/compresión/retención

El proceso del gateway agrega un sink de loguru **al archivo** `daemon_logs_path()`:

```python
logger.add(
    daemon_logs_path(),
    serialize=True,                       # JSONL: una línea JSON por evento
    rotation=f"{max_file_mb} MB",
    retention=f"{retention_days} days",
    compression="gz",                     # segmentos rotados → .gz
    level="INFO",
    enqueue=True,                         # thread/proc-safe
    filter=lambda r: r["extra"].setdefault("channel", "-") or True,
)
```

- El sink de **stderr** (texto coloreado humano) **se mantiene** para foreground /
  `--verbose`. Solo el **archivo** pasa a JSONL.
- Gateado para que solo el run del gateway agregue el sink de archivo (no la CLI
  común). Mecanismo: `start_daemon` setea un env var (p. ej.
  `DURIN_GATEWAY_LOG_FILE=1`) que el arranque del gateway detecta para agregar el
  sink; el run foreground del usuario lo agrega también cuando corre como gateway.

### 3.3 Captura de stdout/stderr crudo

Hoy `start_daemon` redirige stdout/stderr del subproceso a `gateway.log`
([`gateway_daemon.py:150-167`](../../../durin/cli/gateway_daemon.py)). Como ahora
loguru es **dueño** de `gateway.log` (para poder rotar/comprimir sin conflicto de fd):

- `start_daemon` redirige stdout/stderr del hijo a un archivo aparte
  `~/.durin/logs/gateway.boot.log`, abierto en modo truncado (`"wb"`) por arranque —
  red de seguridad para fallos catastróficos previos a que loguru levante (errores de
  import, tracebacks tempranos). Acotado por naturaleza (se trunca cada arranque).
- Se instala un `sys.excepthook` que enruta excepciones no capturadas a loguru, así
  los tracebacks de runtime quedan en `gateway.log` estructurados.
- El `logging_bridge` existente ([`durin/utils/logging_bridge.py`](../../../durin/utils/logging_bridge.py))
  ya enruta `logging` de stdlib → loguru, así que las librerías de terceros caen en
  el sink JSONL.

### 3.4 Sin cambios al tail CLI

`durin gateway logs` ([`commands.py:1139-1160`](../../../durin/cli/commands.py)) sigue
tailéando `gateway.log`; ahora muestra JSONL. (Mejorar ese tail a pretty-print queda
fuera de alcance; no es bloqueante.)

## 4. Parte B — Visor web

### 4.1 El primitivo de lectura compartido (el corazón del diseño)

Un único módulo server-side (`durin/logs/reader.py`) que los dos tabs comparten.
Principio: **el filesystem ya es el índice temporal** — los archivos rotan por
tiempo y dentro de cada uno las líneas están en orden ascendente de `ts`. No hay
orden global ni base de datos.

Reglas que hacen que el costo escale con la **página**, no con el **corpus**:

1. **Nuevo-primero + corte temprano.** Se streamea desde el segmento más nuevo hacia
   atrás; se corta apenas se juntan `limit` matches. Consultas típicas tocan 1–2
   segmentos (~100 ms) sin importar el tamaño del histórico.
2. **Paginación por cursor temporal.** "Cargar más viejo" usa `before_ts` (timestamp
   de la última línea mostrada), no `offset`. Reanuda donde quedó → O(página) por
   página, no O(n²).
3. **Grep-antes-de-parsear.** Substring crudo sobre el texto de la línea primero;
   `json.loads` solo en las líneas que pasan el filtro. Evita parsear el 99% que no
   matchea.
4. **Descompresión `.gz` transparente.** Un helper abre `.gz` (`gzip.open`) o plano
   (`open`) y devuelve líneas; el resto del código no distingue. Un segmento de 5 MB
   se descomprime en ms.
5. **Ventana acotada por defecto + ampliar explícito.** El search corre por defecto
   sobre una ventana reciente (p. ej. últimas 24 h / N segmentos) con un **presupuesto**
   de escaneo. La respuesta incluye hasta dónde se escaneó (`scanned_through_ts`) y si
   hay más (`has_more`). La UI ofrece *"ampliar"*. **Nunca** un escaneo ilimitado
   silencioso que cuelgue el visor.

**Facetas sin escanear datos** (clave para evitar el full-scan):

| Faceta | Origen | Costo |
|---|---|---|
| Nivel (gateway) | enum fijo DEBUG/INFO/WARN/ERROR | 0 |
| Canal (gateway) | distintos del segmento más nuevo | ~1 archivo |
| Tipo (telemetría) | registro `EVENT_TYPES` de `schema.py` | 0 |
| Sesión (telemetría) | parseada del **nombre de archivo** | listado de dir |

### 4.2 API

Un único endpoint REST en
[`durin/channels/websocket.py`](../../../durin/channels/websocket.py), agregado al
dispatch de `_dispatch_http()`, con token Bearer (`_check_api_token`):

```
GET /api/logs?source=gateway|telemetry
              &level=&channel=&session=&type=       (filtros según source)
              &q=<substring>
              &before_ts=<cursor>                   (paginación temporal)
              &window_hours=24                       (ventana de escaneo; ampliable)
              &limit=200
```

Respuesta:

```json
{
  "lines": [ { "ts": <float>, "level": "...", "channel": "...", "message": "...", "raw": {...} } ],
  "facets": { "levels": [...], "channels": [...], "sessions": [...], "types": [...] },
  "next_cursor": <ts or null>,
  "scanned_through_ts": <float>,
  "has_more": <bool>
}
```

### 4.3 Estructura UI

- Nueva entrada `logs` en
  [`webui/src/components/settings/SettingsView.tsx`](../../../webui/src/components/settings/SettingsView.tsx)
  (ícono `ScrollText`): agregar a `SettingsSectionKey`, a `SETTINGS_NAV_ITEMS`, y el
  caso de dispatch.
- Nuevo `webui/src/components/settings/LogsSettings.tsx`:
  - Tabs **Gateway** | **Telemetría**; barra de filtros sticky; botón **Refrescar**.
  - Tabla densa monoespaciada, orden por fecha descendente.
  - Paginación "cargar más viejo" por cursor (`before_ts` = `next_cursor`).
  - Banner *"buscado en las últimas 24 h — [ampliar]"* cuando `has_more`.
  - Click en fila → expande el JSON crudo (`raw`).
  - Header del tab Gateway: las perillas `max_file_mb` / `retention_days` editables
    (vía `/api/config/set`). El tab Telemetría **no** muestra config.
- Función `fetchLogs(token, query)` en
  [`webui/src/lib/api.ts`](../../../webui/src/lib/api.ts) sobre el wrapper `request<T>`.

### 4.4 Diferencias entre tabs (lo único que NO es compartido)

| | Gateway | Telemetría |
|---|---|---|
| Directorio/glob | `~/.durin/logs/gateway.log*` | `~/.cache/durin/telemetry/*.jsonl*` |
| Parser de línea | JSONL loguru (`time`,`level`,`extra.channel`,`message`) | JSONL telemetría (`ts`,`type`,`data`) |
| Facetas | nivel (enum), canal (segmento nuevo) | tipo (`EVENT_TYPES`), sesión (filename) |
| Columnas | fecha · nivel · canal · mensaje | fecha · sesión · tipo · resumen de `data` |

## 5. Plan de pruebas (gates)

- **Rotación gateway:** escribir > `max_file_mb` y verificar que se crea un segmento
  nuevo y que el rotado queda `.gz`.
- **Retención gateway:** segmento `.gz` con mtime > `retention_days` se borra.
- **Formato JSONL:** el archivo emite una línea JSON por evento con
  `time/level/extra.channel/message`; el stderr sigue en texto humano.
- **Config:** `logging.max_file_mb` / `logging.retention_days` se leen y se setean vía
  `/api/config/set`.
- **Captura crudo:** un traceback no capturado aparece en `gateway.log` (vía excepthook).
- **Reader / primitivo:**
  - lee `.gz` y plano transparente; orden nuevo-primero;
  - `before_ts` reanuda sin solapamiento ni saltos;
  - grep-antes-de-parsear no devuelve líneas que no matchean;
  - `window_hours` acota y reporta `has_more` + `scanned_through_ts`;
  - facetas de telemetría salen de filename/`EVENT_TYPES`, no de escanear.
- **API `/api/logs`:** 401 sin token; filtros por source; search; paginación por
  cursor; ambos sources; lee `.gz` de telemetría.
- **Subprocess/CI env:** la migración del gateway a loguru se verifica bajo
  `HOME=/tmp/empty` + `GIT_CONFIG_NOSYSTEM=1`.
- **Webui (verificación viva):** abrir la sección Logs, ambos tabs, filtros, "cargar
  más viejo", "ampliar", editar las perillas de config del gateway.

## 6. Mapa de archivos

**Backend:**
- `durin/config/schema.py` — `LoggingConfig` (`max_file_mb`, `retention_days`) + campo en `Config`.
- `durin/cli/commands.py` — agregar el sink de archivo JSONL en el arranque del gateway (gateado).
- `durin/cli/gateway_daemon.py` — redirigir stdout/stderr a `gateway.boot.log`; setear env var.
- `durin/logs/reader.py` — **nuevo**, el primitivo de lectura compartido (open transparente, stream, grep-antes-de-parsear, cursor, ventana, facetas).
- `durin/channels/websocket.py` — `_handle_logs_list` + dispatch.

**Frontend:**
- `webui/src/components/settings/SettingsView.tsx` — nav + dispatch `logs`.
- `webui/src/components/settings/LogsSettings.tsx` — **nuevo**, dos tabs + filtros + cursor + ampliar.
- `webui/src/lib/api.ts` — `fetchLogs`.

**Docs:**
- `docs/ARCHITECTURE.md` — actualizar (toca channels + un módulo nuevo `logs/`).

**Explícitamente NO se toca:** `durin/telemetry/logger.py`, `durin/telemetry/retention.py`.
