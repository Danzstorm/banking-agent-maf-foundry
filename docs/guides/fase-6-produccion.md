# Fase 6: producción

El agente host deja de correr en tu máquina y pasa a Azure Container Apps como servicio HTTP sin estado. Las conversaciones viven en Azure SQL, las llamadas al modelo y al MCP se reintentan con backoff, y el MCP queda accesible solo desde dentro del entorno. Decisiones: [ADR-013](../DECISIONS.md#adr-013-sesiones-durables-en-azure-sql), [ADR-014](../DECISIONS.md#adr-014-capa-de-resiliencia-propia) y [ADR-017](../DECISIONS.md#adr-017-guardrails-de-contenido-con-indirect-attack-habilitado).

> **Estado: hecha, con la prueba de carga pendiente.** Despliegue, probes, sesiones durables, resiliencia, pruebas de caos y RAI policy están verificados (2026-11-08). La prueba de carga contra Azure no se ha corrido: requiere un inicio de sesión interactivo por device code ([cómo correrla](#prueba-de-carga)).

## Qué se construyó

### Agente host en Container Apps

`agent_host/service.py` expone el host como API HTTP (Starlette):

| Ruta | Para qué |
|------|----------|
| `POST /chat` | `{conversation_id?, message?, approvals?}` -> `{conversation_id, text, replies, pending_approvals}` |
| `GET /healthz` | Liveness: el proceso responde. Sin dependencias. |
| `GET /readyz` | Readiness: el host terminó de construirse. No consulta SQL a propósito: una probe cada pocos segundos impediría que la base free offer se pause sola. |

La Container App `agent-host` (`infra/apps.tf`) usa la misma imagen que `mcp-server` con otro comando (`python -m agent_host.service`), escucha en 8080, escala de 0 a 2 réplicas (regla HTTP de 10 solicitudes concurrentes) y define las probes de liveness (`/healthz`, cada 10 s) y readiness (`/readyz`, cada 5 s). Su ingress es público: el cliente llega con un token Entra para `access_as_user`, que el host valida con el mismo código del MCP antes de hacer OBO ([fase 5](fase-5-identidad.md)).

### Sesiones durables

Cada request reconstruye agentes y workflow y restaura la conversación desde su checkpoint en la tabla `sesiones`, incluidas las aprobaciones pendientes. Cualquier réplica atiende cualquier turno, también aprobar una escritura que propuso otra réplica antes de morir. Si el checkpoint de una aprobación abierta no se guardó, el host responde 503 en lugar de devolver una aprobación que no podría retomarse.

### Resiliencia

`agent_host/resilience.py` (MAF no trae retry ni circuit breaker):

| Pieza | Qué hace |
|-------|----------|
| `RetryChatMiddleware` | Reintenta llamadas al modelo ante 408, 429, 5xx, timeouts y errores de conexión: 4 intentos, espera `1 s * 2^n` (máximo 8 s) más hasta 1 s de jitter. Los reintentos del SDK de OpenAI están desactivados (`max_retries=0`), así que esta es la única política. |
| `ResilientTools` | Reintenta llamadas a herramientas ante errores de transporte: 3 intentos desde 0,5 s. Reintentar escrituras del MCP es seguro porque son idempotentes ([ADR-007](../DECISIONS.md#adr-007-idempotencia-con-idempotency_key)). |
| `CircuitBreaker` | Protege la búsqueda en la base de conocimiento: tras 3 fallas seguidas se abre 30 s y el modelo recibe un mensaje que le indica ofrecer un asesor (`escalate_to_human`) en lugar de esperar. Después deja pasar una llamada de prueba. El estado vive en cada réplica. |
| `db_retry` | Reintenta operaciones SQL ante errores transitorios (por ejemplo 40613 mientras la base free offer se reanuda): 6 intentos, de 1 a 20 s. |

Timeouts: 60 s por llamada al modelo, 30 s por request al MCP, 20 s a la base de conocimiento y 120 s por turno completo. Un turno que agota el tiempo responde 504, y uno que falla después de los reintentos responde 503 y queda registrado en el log con `log.exception`. En ambos casos el texto ofrece hablar con un asesor.

### Límites de entrada

- Rate limit por usuario (token bucket): 20 solicitudes por minuto con ráfaga de 10. Al excederlo responde 429 con `Retry-After: 60`. El contador vive en cada réplica.
- Mensaje de hasta 2000 caracteres y body de hasta 16 KB: si se excede, responde 413.
- Se configuran con `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST` y `MAX_MESSAGE_CHARS`.

### MCP con ingress interno

El ingress de `mcp-server` ahora es interno (`external_enabled = false`). Su FQDN pasa a `mcp-server.internal.<dominio del entorno>`: solo lo alcanzan las apps del mismo entorno, como el agente host, y nunca internet. La validación JWT del MCP ([ADR-010](../DECISIONS.md#adr-010-validación-jwt-en-el-código-del-servidor-mcp)) sigue corriendo en cada llamada.

Por qué no basta una lista de IP permitidas: las llamadas entre apps de un mismo entorno de Container Apps llegan al ingress desde IP internas del entorno, no desde las IP de salida públicas del host. Una regla `ip_security_restriction` con las `outbound_ip_addresses` del host nunca coincide y el MCP responde 403. Además, en un entorno de consumo esas IP de salida son compartidas y pueden cambiar. El ingress interno resuelve las dos cosas sin regla de IP.

Consecuencia: desde tu máquina ya no puedes llamar al MCP desplegado. Para pruebas manuales usa un MCP local (`MCP_URL=http://127.0.0.1:8000/mcp`).

### Usuario SQL de mínimo privilegio

El host se conecta con su managed identity `id-agent-host` (`Authentication=ActiveDirectoryMSI`, sin contraseñas). [`infra/sql/create-agent-host-user.sql`](../../infra/sql/create-agent-host-user.sql) le da solo:

- `SELECT` sobre `clientes` (mapear `oid` -> cliente).
- `SELECT`, `INSERT` y `UPDATE` sobre `sesiones`.

Nunca lee cuentas, tarjetas ni reclamos: eso pasa siempre por el MCP.

### RAI policy

`banking-agent-guardrails` (`infra/foundry.tf`, base `Microsoft.DefaultV2`) bloquea Jailbreak e Indirect Attack sobre el prompt, además de los filtros de contenido por defecto.

## Pasos

Requisitos: la infraestructura y las variables de la [fase 5](fase-5-identidad.md).

### 1. Imagen e infraestructura

En `infra/dev.tfvars`, `mcp_image` y `agent_image` apuntan a la misma imagen:

```hcl
mcp_image   = "acrbankingagentke9ih.azurecr.io/mcp-server:v5"
agent_image = "acrbankingagentke9ih.azurecr.io/mcp-server:v5"
```

```bash
ACR=$(terraform -chdir=infra output -raw acr_name)
az acr build --registry "$ACR" --image mcp-server:v5 .
terraform -chdir=infra plan -var-file=dev.tfvars -out=phase6.tfplan
terraform -chdir=infra apply phase6.tfplan
```

### 2. Usuario SQL del host

Conéctate a `sqldb-banking` como administrador Entra y ejecuta el script una vez:

```bash
SQL=$(terraform -chdir=infra output -raw sql_server_fqdn)
sqlcmd -S "$SQL" -d sqldb-banking --authentication-method ActiveDirectoryDefault -i infra/sql/create-agent-host-user.sql
```

### 3. Verificar

```bash
HOST=$(terraform -chdir=infra output -raw agent_host_url)
curl -s "$HOST/healthz"   # {"status":"ok"}
curl -s "$HOST/readyz"    # {"status":"ready",...}
uv run python -m agent_host --remote "$HOST"   # chat con device code (AUTH_MODE=entra y ENTRA_*)
```

La primera petición puede tardar: la app escala desde 0 réplicas y la base puede estar pausada. `terraform plan -var-file=dev.tfvars` debe mostrar que no hay cambios.

Logs del host:

```bash
az containerapp logs show -g "$(terraform -chdir=infra output -raw resource_group)" -n agent-host --follow
```

## Pruebas

| Prueba | Qué comprueba | Azure |
|--------|---------------|-------|
| `tests/contract/test_chaos.py` | Dos procesos host reales comparten la base con el MCP real. Una aprobación pendiente sobrevive a matar la réplica que la propuso, y una réplica muerta a mitad de una escritura aprobada la completa una sola vez en la otra. | No |
| `tests/unit/test_resilience.py` | Un 429 simulado se reintenta con backoff exponencial y jitter; el circuit breaker se abre tras 3 fallas, responde con el fallback sin llamar a la herramienta y pasa a semiabierto tras la espera. | No |

```bash
uv run pytest -q                                          # todo lo local
uv run pytest -q -m contract tests/contract/test_chaos.py # caos
uv run pytest -q tests/unit/test_resilience.py            # resiliencia
```

La prueba de caos usa `kill()` (SIGKILL o TerminateProcess): no corre ningún hook de cierre, así que lo que sobrevive es lo que ya estaba en `sesiones`.

### Prueba de carga

`tests/load/locustfile.py` simula clientes que hacen preguntas de solo lectura al host desplegado con un mismo token. Todos comparten cliente, así que el rate limit por usuario forma parte de lo medido: los 429 se cuentan aparte de las fallas reales.

Inicia sesión **antes** de lanzar locust y pásale el token en `AGENT_TOKEN`. Si locust inicia sesión dentro de `test_start`, la espera del device code consume el tiempo de `-t` y la corrida termina con 0 solicitudes.

```bash
export AUTH_MODE=entra
export ENTRA_AUTHORITY=$(terraform -chdir=infra output -raw entra_authority)
export ENTRA_CLI_CLIENT_ID=$(terraform -chdir=infra output -raw cli_client_id)
export ENTRA_AGENT_API_CLIENT_ID=$(terraform -chdir=infra output -raw agent_api_client_id)
HOST=$(terraform -chdir=infra output -raw agent_host_url)

# El device code se muestra en stderr; solo el token va a la variable.
export AGENT_TOKEN=$(uv run python -c "
import contextlib, sys
from agent_host.cli import remote_auth
with contextlib.redirect_stdout(sys.stderr):
    identity, _ = remote_auth()
print(identity.user_token())
")

uv run --group load locust -f tests/load/locustfile.py --headless -u 5 -r 1 -t 3m \
  --host "$HOST" --csv docs/load/phase6 --only-summary
unset AGENT_TOKEN
```

El token dura alrededor de una hora: obtén uno nuevo para cada corrida. Locust escribe latencias (p50, p95, p99) y tasa de errores en `docs/load/phase6_stats.csv`.

## Problemas frecuentes

| Síntoma | Causa probable |
|---------|----------------|
| `/readyz` responde 503 `starting` | El host aún se está construyendo; si no pasa, revisa los logs (falta una variable o el certificado OBO). |
| `/chat` responde 503 "Servicio no disponible." | El modelo o el MCP fallaron después de los reintentos. El log del host tiene la excepción (`turn failed: conversation ...`). |
| `/chat` responde 504 | El turno superó 120 s. Suele pasar en el primer turno con la base pausada; reintenta. |
| `/chat` responde 429 | Rate limit por usuario (20 por minuto, ráfaga de 10). Espera `Retry-After`. |
| 403 del MCP con una regla de IP | Una lista de IP permitidas no sirve entre apps del mismo entorno: usa el ingress interno (ver arriba). |
| No puedes llamar al MCP desde tu máquina | Es lo esperado: el ingress es interno. Usa un MCP local en modo `dev`. |
| `Login failed for user '<token-identified principal>'` en el host | Falta el paso 2 (usuario `id-agent-host`). |
| Locust termina con 0 solicitudes | Inició sesión dentro de la corrida y el device code consumió `-t`. Usa `AGENT_TOKEN`. |
| Locust responde 401 en todo | `AGENT_TOKEN` venció o es de otra app. Obtén uno nuevo. |
