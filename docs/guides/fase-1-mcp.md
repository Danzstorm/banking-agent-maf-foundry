# Fase 1: core bancario simulado y servidor MCP

En esta fase construyes un servidor MCP local sobre un core bancario simulado. No usa LLM ni Azure: todo corre en tu máquina con SQLite.

## Qué se construyó

| Pieza | Qué hace |
|-------|----------|
| Esquema SQL | Las tablas del [modelo de datos](../ARCHITECTURE.md#7-modelo-de-datos), con SQLAlchemy Core. Para Azure SQL solo cambia `DATABASE_URL` (ver [fase 1.5](fase-1-5-azure.md)). |
| Seed | Datos sintéticos reproducibles de Banco Andino Demo: 10 clientes, cuentas, tarjetas, 60 días de movimientos y algunos reclamos. Semilla y fecha de referencia fijas (último día con movimientos: 2026-11-01). |
| Servidor MCP | Streamable HTTP en `http://127.0.0.1:8000/mcp` con 5 herramientas. |
| Identidad | Una sola función, `resolve_customer`, convierte la petición en un `customer_id`. Ninguna herramienta recibe `customer_id`. |
| Idempotencia | `block_card` y `open_claim` guardan su resultado en `idempotency_keys` (clave primaria = `UNIQUE`). Un reintento devuelve el resultado original sin reejecutar. |
| Auditoría | Cada `tools/call` queda en `auditoria`, incluidas las rechazadas. |

### Herramientas

| Herramienta | Tipo | Notas |
|-------------|------|-------|
| `get_balance()` | Lectura | Cuentas, saldos y tarjetas (con `card_id`) del cliente. |
| `list_transactions(date_from, date_to)` | Lectura | Fechas `AAAA-MM-DD`, ambas incluidas, rango máximo de 90 días. |
| `get_claim_status(claim_id)` | Lectura | Solo reclamos del cliente. |
| `block_card(card_id)` | Escritura | Requiere `X-Conversation-Id`. Si ya estaba bloqueada, responde lo mismo. |
| `open_claim(motivo)` | Escritura | Requiere `X-Conversation-Id`. Motivo de 10 a 500 caracteres. |

La aprobación humana de las escrituras la aplica el agente en la fase 2; aquí las descripciones las marcan como escrituras.

Una tarjeta o un reclamo de otro cliente responde igual que uno inexistente ("no encontrado"), para no revelar que existe.

### Identidad en la fase 1

| `AUTH_MODE` | Comportamiento |
|-------------|----------------|
| `dev` | Lee el header `X-Dev-Customer-Id` (id numérico de `clientes`). Solo para desarrollo local. |
| Cualquier otro valor o sin definir | Rechaza toda llamada a herramienta ("No autorizado."). |

En la fase 5 el cuerpo de `resolve_customer` se reemplaza por validación del JWT de Entra (`oid` -> `customer_id`).

### Clave de idempotencia

`sha256(conversation_id + customer_id + tool + args normalizados)`. Se agrega `customer_id` a la fórmula del ADR-007 para que dos clientes con el mismo `conversation_id` nunca compartan un resultado. En `open_claim` el motivo se normaliza (espacios y mayúsculas) antes de calcular la clave.

## Cómo ejecutarlo

Requisitos: [uv](https://docs.astral.sh/uv/). uv instala Python 3.12 si no lo tienes.

```bash
uv sync
cp .env.example .env
uv run --env-file .env python -m mcp_server.seed   # crea data/banco.db desde cero
uv run --env-file .env python -m mcp_server        # http://127.0.0.1:8000/mcp
```

Sin `--env-file`, `AUTH_MODE` no está definido y el servidor rechaza todas las llamadas: falla cerrado a propósito.

Pruebas y lint:

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
uv run pytest -m contract   # solo las pruebas de contrato MCP
```

## Probar con MCP Inspector

1. Con el servidor corriendo, abre el Inspector:

   ```bash
   npx @modelcontextprotocol/inspector
   ```

2. En la interfaz web elige **Transport Type: Streamable HTTP** y la URL `http://127.0.0.1:8000/mcp`.
3. En la sección de autenticación o headers agrega:
   - `X-Dev-Customer-Id: 1` (cualquier id del 1 al 10).
   - `X-Conversation-Id: inspector-1` (necesario para `block_card` y `open_claim`).
4. Conecta, abre **Tools**, lista las herramientas y ejecuta `get_balance`.
5. Prueba el acceso cruzado: con `X-Dev-Customer-Id: 1`, llama `block_card` con el `card_id` de otro cliente. Debe responder "Tarjeta no encontrada."
6. Llama `open_claim` dos veces con el mismo motivo y el mismo `X-Conversation-Id`: recibes el mismo `claim_id`.

Para revisar la auditoría:

```bash
uv run python -c "import sqlite3; [print(r) for r in sqlite3.connect('data/banco.db').execute('select * from auditoria order by id desc limit 10')]"
```

## Qué hace cada archivo

| Archivo | Contenido |
|---------|-----------|
| `pyproject.toml` | Proyecto uv, dependencias, configuración de ruff y pytest. |
| `mcp_server/db.py` | Esquema (tablas en español) y `make_engine` a partir de `DATABASE_URL`. |
| `mcp_server/auth.py` | `resolve_customer`: el único punto donde la petición se convierte en cliente. |
| `mcp_server/banking.py` | Lógica de las herramientas: consultas, pertenencia, validación de rangos e idempotencia. No depende de MCP. |
| `mcp_server/server.py` | Servidor MCP: registra las 5 herramientas y el middleware de auditoría. |
| `mcp_server/__main__.py` | Arranca el servidor en streamable HTTP (`MCP_HOST`, `MCP_PORT`). |
| `mcp_server/seed.py` | Genera los datos sintéticos (recrea la base). |
| `tests/conftest.py` | Base temporal con dos clientes (A y B) para las pruebas. |
| `tests/unit/` | Pruebas de herramientas, autorización, idempotencia, identidad y reproducibilidad del seed. |
| `tests/contract/` | Levanta el servidor real y lo usa con el cliente MCP oficial por streamable HTTP. |
| `.github/workflows/ci.yml` | ruff y pytest en cada push y pull request, sin Azure. |
| `.env.example` | Variables de entorno de ejemplo, sin secretos. |
