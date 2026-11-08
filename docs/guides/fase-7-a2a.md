# Fase 7: A2A

El agente Productos deja de correr dentro del agente host y pasa a su propia Container App, expuesto como servidor A2A 1.0. El host lo llama por JSON-RPC con un token Entra de su managed identity, protegido por el circuit breaker, y las dos mitades de cada llamada quedan en una sola traza de Application Insights. Decisiones: [ADR-015](../DECISIONS.md#adr-015-agente-productos-remoto-vía-a2a), [ADR-016](../DECISIONS.md#adr-016-propagación-manual-de-trazas-en-a2a) y [ADR-014](../DECISIONS.md#adr-014-capa-de-resiliencia-propia).

> **Estado: desplegada y verificada, con tres criterios pendientes.** La card, `SendMessage` directo con token de managed identity, el rechazo sin token, la prueba de interoperabilidad y la propagación de la traza entre las dos apps están verificados en Azure (2026-11-08). Quedan pendientes la conversación completa por `/chat` (requiere inicio de sesión interactivo por device code), la prueba con Productos detenido sobre el despliegue y la verificación manual con A2A Inspector ([pendientes](#pendientes)).

> **Componente experimental.** `agent-framework-a2a` está en beta (`1.0.0b261106`, con `a2a-sdk` 1.3). Las firmas pueden cambiar entre versiones; revisa esta fase en cada actualización.

## Qué se construyó

### Servidor A2A: `productos-a2a`

`agent_host/productos_a2a.py` envuelve el mismo agente Productos de la [fase 3](fase-3-knowledge.md) (solo la base de conocimiento, sin datos del cliente) con `agent_framework.a2a.A2AExecutor`, el `DefaultRequestHandler` de `a2a-sdk` y las rutas Starlette `create_agent_card_routes` y `create_jsonrpc_routes`:

| Ruta | Para qué | Token |
|------|----------|-------|
| `GET /.well-known/agent-card.json` | Agent card: skill, endpoint JSON-RPC y esquema de seguridad `bearer` | No |
| `POST /` | JSON-RPC 2.0, método `SendMessage`, header `A2A-Version: 1.0` | Sí |
| `GET /healthz` | Liveness y readiness, sin dependencias | No |

La respuesta es una tarea (`task`) en estado `TASK_STATE_COMPLETED` cuyo mensaje del agente es el JSON `{"answer", "sources"}` de [ADR-018](../DECISIONS.md#adr-018-respuestas-consistentes-y-fundamentadas). Sin header `A2A-Version`, el SDK interpreta la versión 0.3 y responde el error JSON-RPC `-32009` (versión no soportada).

La Container App `productos-a2a` (`infra/a2a.tf`) usa la misma imagen que `agent-host` y `mcp-server` con otro comando (`python -m agent_host.productos_a2a`), escucha en 8090, escala de 0 a 1 réplica, tiene ingress **interno** y probes de liveness y readiness sobre `/healthz`. Su identidad `id-productos-a2a` tiene solo los roles que Productos necesitaba en el host: `AcrPull`, `Foundry User` sobre la cuenta de Foundry y `Search Index Data Reader` sobre AI Search.

### Autenticación servicio a servicio

El token es del tenant **workforce** de la suscripción, no del tenant External ID de los clientes: Productos no necesita la identidad del cliente, así que no hay segunda cadena OBO (ADR-015).

| Pieza | Dónde | Qué hace |
|-------|-------|----------|
| App `banking-productos-a2a` | `infra/a2a.tf`, provider `azuread` por defecto | Audiencia del token (`api://<client-id>`). Expone el app role `Productos.Invoke` para aplicaciones y emite tokens v2. |
| `app_role_assignment_required = true` | Service principal de la app | Entra no emite tokens para esta audiencia a identidades sin asignación. |
| Asignación del rol | `azuread_app_role_assignment` | Solo la managed identity `id-agent-host` tiene `Productos.Invoke`. |
| Cliente | `agent_host/productos.py` (`BearerAuth`) | El host pide `api://<client-id>/.default` con su managed identity y agrega `Authorization: Bearer` a cada request del cliente A2A. El token se cachea hasta 5 minutos antes de vencer. |
| Validación | `entra_validator` + `validate_jwt` de `mcp_server/auth.py` | Firma RS256 contra las claves del tenant, `aud`, `iss`, `tid`, vigencia y el rol en `roles`. Cualquier falla responde 401 con `WWW-Authenticate: Bearer`. |

`validate_jwt` ahora acepta `scope` (tokens delegados, claim `scp`: MCP y API del agente) o `role` (tokens de aplicación, claim `roles`: Productos). El validador acepta los dos formatos de token de la misma app (v2: `aud` es el client id e `iss` es `login.microsoftonline.com/<tid>/v2.0`; v1: `aud` es `api://<client-id>` e `iss` es `sts.windows.net/<tid>/`) para que un cambio del manifiesto no deje al host afuera. En Azure el token llega en v2.

La card declara el esquema (`securitySchemes.entra.httpAuthSecurityScheme.scheme = "bearer"`) y es pública dentro del entorno: es el documento de descubrimiento. Lo que se protege es `SendMessage`.

### Cliente: Productos como proxy A2A

`HandoffBuilder` solo acepta participantes `Agent` locales, así que `A2AAgent` no puede entrar directo al workflow. El participante `productos` pasa a ser un **proxy** (`build_productos_proxy`): un agente local con el mismo nombre, la misma descripción para el handoff y salida `ProductAnswer`, cuya única herramienta de producto es `consultar_productos`, el `A2AAgent` remoto expuesto con `as_tool()`. Conserva `escalate_to_human`.

`agent_host/service.py` elige el modo al arrancar:

| `PRODUCTOS_A2A_URL` | Productos | El circuit breaker protege |
|---------------------|-----------|----------------------------|
| Definida | Proxy A2A hacia `productos-a2a` | `consultar_productos` (la llamada A2A) |
| Vacía | Agente en proceso, como en la fase 6 | `knowledge_base_retrieve` |

`PRODUCTOS_A2A_SCOPE` (`api://<client-id>/.default`) es obligatoria con `PRODUCTOS_A2A_URL`. `/readyz` informa el modo: `{"status": "ready", "productos": "a2a", ...}`. El cliente A2A y su pool HTTP se crean una vez por réplica y se cierran al apagar.

### Circuit breaker y escalamiento

Se reutiliza `ResilientTools` de la fase 6, ahora sobre `consultar_productos`: 3 intentos ante errores de transporte, y tras 3 fallas seguidas el circuito se abre 30 s. Abierto, o si la llamada falla después de los reintentos, el modelo recibe `BREAKER_FALLBACK`, que le indica no responder datos de productos y ofrecer un asesor (`escalate_to_human`). Una tarea remota que termina en `TASK_STATE_FAILED` no cuenta como falla del breaker: llega como texto al proxy. Del lado del servidor, la búsqueda en la base de conocimiento mantiene su propio breaker.

### Trazas distribuidas

`agent_host/telemetry.py` configura Azure Monitor OpenTelemetry en las dos apps (solo si existe `APPLICATIONINSIGHTS_CONNECTION_STRING`) e instrumenta HTTPX en el cliente y Starlette en el servidor (ADR-016). El `traceparent` viaja en el request A2A y los spans del servidor quedan bajo la traza del host. Cada `POST /chat` es una traza con el request del host, la dependencia HTTP hacia `productos-a2a` y el request del servidor con sus llamadas a Foundry y a AI Search.

Para no agotar la cuota de 1 GB diario de Log Analytics:

- Solo se exportan los logs de `agent_host.*` (`logger_name`).
- Las probes (`/healthz`, `/readyz`) no se trazan (`OTEL_PYTHON_STARLETTE_EXCLUDED_URLS`).
- Los spans internos de `a2a-sdk` se desactivan con `OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=false` (en Terraform).

## Pasos

Requisitos: la infraestructura de la [fase 6](fase-6-produccion.md) y permisos para crear app registrations y asignar app roles en el tenant workforce (por ejemplo, Application Administrator).

### 1. Imagen e infraestructura

```bash
ACR=$(terraform -chdir=infra output -raw acr_name)
az acr build --registry "$ACR" --image mcp-server:v8 .
# infra/dev.tfvars: mcp_image y agent_image = "<acr>/mcp-server:v8"
terraform -chdir=infra plan -var-file=dev.tfvars -out=phase7.tfplan
terraform -chdir=infra apply phase7.tfplan
```

El plan crea la identidad, sus tres roles, la app registration con su service principal y la asignación del rol, y la Container App. Actualiza en sitio `agent-host` (imagen y `PRODUCTOS_A2A_URL`/`PRODUCTOS_A2A_SCOPE`) y `mcp-server` (imagen). No destruye nada.

### 2. Verificar el host

```bash
HOST=$(terraform -chdir=infra output -raw agent_host_url)
curl -s "$HOST/readyz"   # {"status":"ready","productos":"a2a","knowledge_breaker":"closed"}
```

### 3. Verificar Productos desde dentro del entorno

El ingress es interno, así que `curl` desde tu máquina no llega. Ejecuta las pruebas dentro del contenedor del host con `az containerapp exec` (la imagen no trae `curl`; usa Python). Para evitar problemas de comillas, codifica el script en base64:

```bash
RG=$(terraform -chdir=infra output -raw resource_group)
cat > /tmp/a2a_check.py <<'EOF'
import json, os, uuid, httpx
from azure.identity import DefaultAzureCredential
url = os.environ["PRODUCTOS_A2A_URL"]
print(httpx.get(url + ".well-known/agent-card.json", timeout=60).json()["name"])
body = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": {
    "messageId": str(uuid.uuid4()), "role": "ROLE_USER",
    "parts": [{"text": "Cual es la comision de mantenimiento de la cuenta corriente?"}]}}}
print(httpx.post(url, json=body, headers={"A2A-Version": "1.0"}, timeout=60).status_code)  # 401
token = DefaultAzureCredential().get_token(os.environ["PRODUCTOS_A2A_SCOPE"]).token
r = httpx.post(url, json=body, timeout=120,
               headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"})
print(json.dumps(r.json()["result"]["task"]["status"]["state"]))  # TASK_STATE_COMPLETED
EOF
B64=$(base64 -w0 /tmp/a2a_check.py)
az containerapp exec -g "$RG" -n agent-host --command "python -c \"import base64;exec(base64.b64decode('$B64'))\"" < /dev/null
```

Resultado verificado: card `productos`, 401 sin token y `TASK_STATE_COMPLETED` con la respuesta y sus fuentes ("S/ 18 al mes [...]", fuentes "Tarifario de Comisiones y Gastos" y "Cuenta Corriente Andina").

### 4. Ver la traza

En Application Insights > Transaction search, o en Log Analytics:

```kusto
union AppRequests, AppDependencies
| where OperationId == "<trace id>"
| project TimeGenerated, Type, AppRoleName, Name, ResultCode, DurationMs
| order by TimeGenerated asc
```

Deben aparecer `agent-host` y `productos-a2a` con el mismo `OperationId`.

Resultado verificado con una llamada instrumentada desde el contenedor del host: una sola traza con el span del cliente, la dependencia `POST /` hacia Productos, el request `POST /` de `productos-a2a` y, debajo, `invoke_agent productos`, las llamadas MCP a la base de conocimiento (`tools/call knowledge_base_retrieve`) y las dos llamadas a `gpt-5-mini`.

## Pruebas

| Prueba | Qué comprueba | Azure |
|--------|---------------|-------|
| `tests/contract/test_a2a_interop.py` | Levanta el servidor A2A real con un modelo guionado y le habla con `httpx` y JSON-RPC escrito a mano, sin MAF ni cliente `a2a-sdk`: `GET` de la card (endpoint, versión, esquema `bearer`), `SendMessage` con respuesta completa, 401 sin token y error de versión sin `A2A-Version`. También comprueba que el cliente del host (`A2AAgent` + `BearerAuth`) recibe la respuesta. | No |
| `tests/unit/test_productos_a2a.py` | Validación del token de managed identity (v1 y v2; rechaza sin rol, con otro rol, token delegado, otra audiencia, otro tenant, tenant External ID, vencido), 401 del servidor con la card pública, cacheo del token del cliente, y Productos caído: la llamada A2A falla, el breaker se abre, el modelo recibe el fallback de escalamiento y el turno siguiente no sale a la red. | No |

```bash
uv run pytest -q                                              # todo lo local
uv run pytest -q -m contract tests/contract/test_a2a_interop.py
uv run pytest -q tests/unit/test_productos_a2a.py
```

## Pendientes

| Criterio | Por qué está pendiente | Cómo cerrarlo |
|----------|------------------------|---------------|
| Una conversación por `/chat` genera una sola traza con las dos apps | Requiere un token de usuario por device code. La propagación entre `agent-host` y `productos-a2a` ya se verificó con una llamada instrumentada desde el contenedor del host. | `uv run python -m agent_host --remote "$HOST"`, una pregunta de productos, y la consulta del paso 4 con el `operation_Id` del request `/chat`. |
| Con Productos detenido, Triage escala a una persona | Cubierto por la prueba unitaria; falta repetirlo sobre el despliegue, que también requiere `/chat`. | `az containerapp ingress disable -g "$RG" -n productos-a2a`, una pregunta de productos (el agente debe ofrecer un asesor) y `az containerapp ingress enable -g "$RG" -n productos-a2a --type internal --target-port 8090 --transport auto`. Después, `terraform plan` debe quedar sin cambios. |
| A2A Inspector conecta y conversa | Verificación manual. El ingress interno no es alcanzable desde tu máquina. | Corre el servidor en local (`uv run python -m agent_host.productos_a2a` con las variables de la sección siguiente) y apunta `ghcr.io/a2aproject/a2a-inspector` a `http://127.0.0.1:8090/` con un token de la app en el header `Authorization`. |

Ejecutar el servidor en local (tu usuario necesita `Foundry User` y lectura en AI Search, como en la fase 3):

```bash
export FOUNDRY_PROJECT_ENDPOINT=$(terraform -chdir=infra output -raw foundry_project_endpoint)
export SEARCH_ENDPOINT=$(terraform -chdir=infra output -raw search_endpoint)
export A2A_PUBLIC_URL=http://127.0.0.1:8090/
export A2A_TENANT_ID=$(az account show --query tenantId -o tsv)
export A2A_AUDIENCE=$(terraform -chdir=infra output -raw productos_a2a_client_id)
uv run python -m agent_host.productos_a2a
```

Con `app_role_assignment_required = true`, tu usuario no obtiene tokens para la app: para el Inspector, asígnate el rol temporalmente o usa el contenedor del host.

## Problemas frecuentes

| Síntoma | Causa probable |
|---------|----------------|
| `curl` a `productos-a2a.internal...` no resuelve desde tu máquina | Es lo esperado: el ingress es interno. Usa `az containerapp exec` en `agent-host`. |
| 401 con un token válido | El token no trae `Productos.Invoke` en `roles` (falta la asignación, o el token se emitió antes de ella: los tokens de managed identity se cachean hasta 24 h), o se pidió otra audiencia. Decodifica el token y revisa `aud`, `iss` y `roles`. |
| Error `AADSTS` al pedir el token, por falta de asignación | La identidad no tiene el rol y la app exige asignación (`app_role_assignment_required`). |
| Error JSON-RPC `-32009` | Falta el header `A2A-Version: 1.0`: el SDK asume 0.3. |
| `SendMessage` devuelve `TASK_STATE_FAILED` | Falló el agente remoto (modelo o base de conocimiento). El texto del error viene en el mensaje de estado; revisa los logs de `productos-a2a`. |
| Miles de filas por hora en `AppTraces` | Se exportan los logs del logger raíz: el exportador registra cada envío y cada registro genera otro envío. Usa `logger_name="agent_host"`, como en `telemetry.py`. |
| Decenas de spans `a2a.server.*` por llamada | Falta `OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=false`. Se lee al importar `a2a-sdk`, así que debe venir del entorno, no del código. |
| `TypeError` al instrumentar Starlette con `excluded_urls` | `StarletteInstrumentor.instrument_app` no acepta ese argumento; las URL excluidas se leen de `OTEL_PYTHON_STARLETTE_EXCLUDED_URLS` al importar el módulo. |
| La primera llamada a Productos tarda | `productos-a2a` escala desde 0 réplicas. El cliente espera hasta 15 s la conexión y 60 s la respuesta. |
