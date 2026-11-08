# 05. Flujos de punta a punta

[Índice de la guía](README.md) · Anterior: [04. Decisiones](04-decisiones.md) · Siguiente: [06. Seguridad](06-seguridad.md)

Cada sección sigue una petición real por el sistema, con un diagrama de secuencia y la tabla "dónde está en el código". Los nombres de funciones y archivos son los del repositorio.

Contenido:

1. [Inicio de sesión y OBO](#1-inicio-de-sesión-y-obo)
2. [Turno de chat con herramienta y aprobación humana](#2-turno-de-chat-con-herramienta-y-aprobación-humana)
3. [Ciclo de vida de una aprobación](#3-ciclo-de-vida-de-una-aprobación)
4. [Sesión durable: una réplica muere](#4-sesión-durable-una-réplica-muere)
5. [Handoff: Triage, Operaciones y Productos](#5-handoff-triage-operaciones-y-productos)
6. [Llamada A2A con token de managed identity y traza](#6-llamada-a2a-con-token-de-managed-identity-y-traza)
7. [Escalamiento y circuit breaker](#7-escalamiento-y-circuit-breaker)
8. [Recuperación de conocimiento con citas](#8-recuperación-de-conocimiento-con-citas)
9. [Idempotencia de una escritura](#9-idempotencia-de-una-escritura)
10. [Ejecución de evaluaciones y CI con OIDC](#10-ejecución-de-evaluaciones-y-ci-con-oidc)
11. [Modelo de datos](#modelo-de-datos)

---

## 1. Inicio de sesión y OBO

El cliente inicia sesión una vez en la CLI. Después, en cada turno, el host valida su token y lo cambia por uno para el MCP.

```mermaid
sequenceDiagram
    autonumber
    actor C as Cliente
    participant CLI as CLI banking-cli
    participant EID as Entra External ID
    participant H as agent-host
    participant KV as Key Vault
    participant M as mcp-server
    participant DB as Azure SQL

    C->>CLI: python -m agent_host --remote HOST
    CLI->>EID: devicecode, scope access_as_user
    EID-->>CLI: user_code y URL de verificación
    CLI-->>C: Abre bancoandinodemo.ciamlogin.com/device e ingresa el código
    C->>EID: Correo y contraseña en el navegador
    CLI->>EID: Consulta el endpoint de token
    EID-->>CLI: Access token aud banking-agent-api
    CLI->>H: POST /chat con Authorization Bearer
    H->>H: validate_jwt con firma RS256, iss, aud, exp, tid, scp
    H->>DB: SELECT clientes por entra_oid
    DB-->>H: customer_id
    Note over H: Al arrancar, OboExchange lee el certificado una sola vez
    H->>KV: Secreto agent-obo con managed identity
    KV-->>H: PEM con clave y certificado
    H->>EID: OBO con el token del cliente y aserción firmada
    EID-->>H: Token aud banking-mcp-api, scp tools.access, mismo oid
    H->>M: tools/call con Bearer OBO y X-Conversation-Id
    M->>M: require_bearer y validate_token
    M->>DB: SELECT clientes por entra_oid
    M-->>H: Resultado solo del cliente
```

| Paso | Dónde está |
|------|------------|
| Device code | `EntraIdentity.sign_in` (`agent_host/identity.py`), llamado por `remote_auth` en `cli.py` |
| Token silencioso en cada turno | `EntraIdentity.user_token` |
| Validación en el host | `entra_authenticator` en `service.py`, con `validate_jwt` de `mcp_server/auth.py` |
| `oid` a cliente en el host | `customer_by_oid` en `sessions.py` (usa el permiso `SELECT` sobre `clientes`) |
| OBO | `OboExchange.mcp_token`; `TokenProvider(lambda: obo.mcp_token(user_token))` |
| Lectura del certificado | `load_certificate_pem` con `SecretClient`; se carga perezosamente la primera vez que se necesita el OBO |
| Header hacia el MCP | `mcp_headers` en `agent.py` |
| Validación en el MCP | `require_bearer` (HTTP 401 antes de abrir la sesión MCP) y `resolve_customer` |

> **Detalle.** MSAL cachea el token OBO de cada cliente y lo renueva cerca del vencimiento. La llamada es síncrona: está marcada con `ponytail:` en `identity.py` como un límite conocido si el host atendiera muchos usuarios.

---

## 2. Turno de chat con herramienta y aprobación humana

```mermaid
sequenceDiagram
    autonumber
    actor C as Cliente
    participant CLI as CLI
    participant H as agent-host /chat
    participant S as SqlCheckpointStorage
    participant W as Workflow de handoff
    participant O as Operaciones
    participant M as mcp-server

    C->>CLI: Bloquea mi tarjeta
    CLI->>H: message
    H->>H: authenticate, TokenBucket, _parse
    H->>S: get_latest
    S-->>H: Checkpoint con HandoffAgentUserRequest pendiente
    H->>W: run con checkpoint_id y la respuesta del usuario
    W->>O: Handoff desde Triage
    O->>M: get_balance
    M-->>O: Tarjetas con card_id
    O-->>W: Llamada block_card que requiere aprobación
    W-->>H: Evento request_info con function_approval_request
    W->>S: save checkpoint de la pausa
    H->>H: Comprueba que el checkpoint guardó la solicitud
    H-->>CLI: pending_approvals con request_id, tool y args
    CLI->>C: El agente quiere ejecutar Bloquear tarjeta. ¿Apruebas?
    C->>CLI: s
    CLI->>H: approvals con request_id y approved true
    H->>S: get_latest
    H->>H: Las aprobaciones coinciden con las pendientes
    H->>W: run con checkpoint_id y function_approval_response
    W->>O: Continúa
    O->>M: block_card
    M-->>O: Tarjeta bloqueada
    O-->>W: Texto final
    W->>S: save checkpoint
    H-->>CLI: text y replies
```

Reglas que aplica el host (`advance` y `chat` en `service.py`):

| Situación | Respuesta |
|-----------|-----------|
| Se envían `message` y `approvals` a la vez, o ninguno | 400 |
| Hay aprobaciones pendientes y llega un mensaje | 409 "Primero responde las aprobaciones pendientes." |
| Las aprobaciones no coinciden con las pendientes | 409 |
| `conversation_id` de otro cliente | 404 "Conversación no encontrada." (no revela de quién es) |
| El checkpoint de una aprobación abierta no se guardó | 503: nunca devuelve una aprobación que no podría retomarse |
| El turno supera 120 s | 504 con oferta de asesor |
| El modelo o el MCP fallan tras los reintentos | 503 con oferta de asesor |

Si el cliente **rechaza**, MAF devuelve a la herramienta el resultado "rejected by user" sin llamar al MCP, y las instrucciones de Operaciones dicen que responda que no se hizo.

---

## 3. Ciclo de vida de una aprobación

```mermaid
stateDiagram-v2
    [*] --> Propuesta: el modelo llama block_card u open_claim
    Propuesta --> Pendiente: MAF emite function_approval_request
    Pendiente --> Persistida: checkpoint guardado en sesiones
    Persistida --> Aprobada: cliente responde approved true
    Persistida --> Rechazada: cliente responde approved false
    Aprobada --> Ejecutada: el MCP ejecuta una vez con idempotency_key
    Rechazada --> Informada: resultado rejected by user sin llamar al MCP
    Ejecutada --> [*]
    Informada --> [*]
```

La aprobación pendiente sobrevive en el estado `Persistida`: cualquier réplica puede recibir la respuesta del cliente.

---

## 4. Sesión durable: una réplica muere

`tests/contract/test_chaos.py` lo prueba con dos procesos host reales que comparten la base, matando uno con `kill()` (sin hooks de cierre).

```mermaid
sequenceDiagram
    autonumber
    actor C as Cliente
    participant R1 as Réplica 1
    participant R2 as Réplica 2
    participant DB as sesiones en Azure SQL
    participant M as mcp-server

    C->>R1: Bloquea mi tarjeta
    R1->>DB: Checkpoint con aprobación pendiente
    R1-->>C: pending_approvals
    Note over R1: La réplica 1 muere
    C->>R2: approvals con approved true
    R2->>DB: get_latest
    DB-->>R2: Checkpoint con la solicitud pendiente
    R2->>R2: Reconstruye agentes y workflow
    R2->>M: block_card con la misma idempotency_key
    M-->>R2: Tarjeta bloqueada
    R2->>DB: Checkpoint final
    R2-->>C: Listo
```

Segundo caso de la prueba: la réplica muere **durante** una escritura aprobada. El checkpoint guardado no tiene solicitudes pendientes pero la conversación no terminó, así que `advance` detecta `latest is not None and not waiting` y vuelve a ejecutar el superstep desde el checkpoint. La escritura se completa **una sola vez** gracias a la idempotencia del MCP.

| Pieza | Dónde |
|-------|-------|
| Un checkpoint por conversación, con su dueño | `SqlCheckpointStorage` (`sessions.py`) |
| Reconstrucción por request | `h.build_participants()` y `build_workflow` en `chat` |
| Reanudar un turno a medias | Primera rama de `advance` |
| Reintentos SQL ante 40613 | `db_retry` |

---

## 5. Handoff: Triage, Operaciones y Productos

Ejemplo: "¿Cuál es mi saldo y cuánto cuesta la Tarjeta Oro?" (escenario "mixta").

```mermaid
sequenceDiagram
    autonumber
    actor C as Cliente
    participant T as Triage
    participant O as Operaciones
    participant P as Productos
    participant F as gpt-5-mini

    C->>T: Mensaje mixto
    T->>F: Instrucciones de Triage y herramientas handoff_to_*
    F-->>T: handoff_to_operaciones
    Note over T,O: El middleware intercepta la llamada. Solo viajan los mensajes de texto
    T->>O: Conversación
    O->>F: Mensaje y herramientas MCP
    F-->>O: get_balance
    O->>O: Ejecuta get_balance
    F-->>O: handoff_to_productos
    Note over O,P: El resultado de get_balance se queda en Operaciones
    O->>P: Conversación
    P->>F: Mensaje y knowledge_base_retrieve o consultar_productos
    F-->>P: Respuesta con fuentes
    P-->>C: Respuesta y HandoffAgentUserRequest
    C->>T: Siguiente mensaje vuelve a Triage
```

| Regla | Implementación |
|-------|----------------|
| Triage solo enruta | `TRIAGE_INSTRUCTIONS`; su única herramienta es `escalate_to_human` |
| Cualquiera transfiere a cualquiera | Sin `add_handoff` (malla) |
| Cada mensaje nuevo pasa por Triage | `enable_return_to_previous(False)` |
| Las respuestas de aprobación vuelven al agente que preguntó | Comportamiento del workflow |
| La identidad llega a todos los agentes | `function_invocation_kwargs` del workflow |

```mermaid
flowchart LR
    t["Triage<br/>escalate_to_human"] <-->|"handoff"| o["Operaciones<br/>6 herramientas MCP"]
    t <-->|"handoff"| p["Productos<br/>conocimiento + escalate_to_human"]
    o <-->|"handoff"| p
```

---

## 6. Llamada A2A con token de managed identity y traza

```mermaid
sequenceDiagram
    autonumber
    participant PX as Proxy productos en agent-host
    participant RT as ResilientTools y breaker
    participant AA as A2AAgent con BearerAuth
    participant MI as Managed identity id-agent-host
    participant EW as Entra workforce
    participant PA as productos-a2a
    participant IQ as Foundry IQ
    participant AI as Application Insights

    PX->>RT: consultar_productos con la pregunta
    RT->>RT: breaker.allow
    RT->>AA: Llamada
    AA->>MI: get_token api://productos/.default
    MI->>EW: Token de aplicación
    EW-->>AA: JWT con roles Productos.Invoke
    AA->>PA: POST / SendMessage, A2A-Version 1.0, Bearer, traceparent
    PA->>PA: RequireToken valida firma, iss, aud, tid y rol
    PA->>PA: A2AExecutor ejecuta el agente Productos
    PA->>IQ: knowledge_base_retrieve
    IQ-->>PA: Fragmentos y referencias
    PA-->>AA: Task TASK_STATE_COMPLETED con answer y sources
    AA-->>RT: Resultado
    RT->>RT: breaker.success
    RT-->>PX: JSON con answer y sources
    PX-->>AI: Spans HTTPX del cliente
    PA-->>AI: Spans Starlette del servidor bajo la misma traza
```

| Aspecto | Valor |
|---------|-------|
| URL interna | `https://productos-a2a.internal.happymoss-084472df.southcentralus.azurecontainerapps.io/` |
| Card | `GET /.well-known/agent-card.json`, sin token |
| Scope pedido | `api://<client id de banking-productos-a2a>/.default` (`PRODUCTOS_A2A_SCOPE`) |
| Emisores aceptados | `https://login.microsoftonline.com/<tid>/v2.0` y `https://sts.windows.net/<tid>/` |
| Audiencias aceptadas | `<client id>` y `api://<client id>` |
| Timeouts del cliente | 15 s de conexión, 60 s de respuesta |
| Cache del token | Hasta 5 minutos antes de vencer |

**Consulta KQL para ver la traza:**

```kusto
union AppRequests, AppDependencies
| where OperationId == "<trace id>"
| project TimeGenerated, Type, AppRoleName, Name, ResultCode, DurationMs
| order by TimeGenerated asc
```

---

## 7. Escalamiento y circuit breaker

Dos caminos llevan a una persona: el **escalamiento por tema** (decidido por el modelo según las instrucciones) y el **escalamiento por falla** (cuando el breaker abre).

```mermaid
sequenceDiagram
    autonumber
    actor C as Cliente
    participant T as Triage
    participant M as mcp-server
    participant DB as Azure SQL

    C->>T: Me robaron la tarjeta y hay cargos que no reconozco
    T->>M: escalate_to_human categoria fraude, sin aprobación
    M->>DB: idempotency_key por conversación y categoría
    M->>DB: INSERT tickets_escalamiento con motivo prefijado fraude
    M-->>T: ticket_id
    T-->>C: Un asesor te contactará
    Note over T: Si hay una tarjeta involucrada, Triage transfiere a Operaciones para ofrecer el bloqueo
```

```mermaid
sequenceDiagram
    autonumber
    participant PX as Proxy productos
    participant B as CircuitBreaker
    participant PA as productos-a2a caído
    participant F as gpt-5-mini

    PX->>B: consultar_productos
    B->>PA: Intento con 3 reintentos desde 0,5 s
    PA--xB: Error de transporte
    B->>B: failure, contador 1
    B-->>PX: BREAKER_FALLBACK
    PX->>F: El servicio no está disponible, ofrece un asesor
    Note over B: Tras 3 fallas seguidas el circuito se abre 30 s
    PX->>B: Nueva pregunta
    B-->>PX: BREAKER_FALLBACK sin salir a la red
    F-->>PX: Ofrece escalate_to_human
```

| Parámetro | Valor | Dónde |
|-----------|-------|-------|
| Umbral | 3 fallas seguidas | `CircuitBreaker(threshold=3)` |
| Tiempo abierto | 30 s, luego semiabierto | `reset_after_s=30` |
| Reintentos de herramienta | 3, desde 0,5 s, máximo 4 s, jitter 1 s | `ResilientTools` |
| Reintentos del modelo | 4, desde 1 s, máximo 8 s | `RetryChatMiddleware` |
| Mensaje al modelo | `BREAKER_FALLBACK` | `resilience.py` |
| Qué protege en el host | `consultar_productos` (A2A) o `knowledge_base_retrieve` (modo local) | `production_host` |
| Qué protege en `productos-a2a` | `knowledge_base_retrieve` | `productos_a2a.main` |
| Estado visible | `knowledge_breaker` en `/readyz` | `service.py` |

Una tarea A2A que termina en `TASK_STATE_FAILED` **no** cuenta como falla del breaker: llega como texto al proxy.

---

## 8. Recuperación de conocimiento con citas

```mermaid
sequenceDiagram
    autonumber
    participant P as Agente Productos
    participant T as SearchTokenHeaders
    participant KB as kb-productos endpoint MCP
    participant S as AI Search
    participant E as text-embedding-3-small
    participant F as gpt-5-mini

    P->>F: Pregunta e instrucciones
    F-->>P: knowledge_base_retrieve con la pregunta completa
    P->>T: header_provider
    T-->>P: Bearer para search.azure.com, cacheado
    P->>KB: tools/call knowledge_base_retrieve
    KB->>E: Vectoriza la consulta
    KB->>S: Búsqueda BM25 y vectorial en paralelo
    S-->>KB: Resultados fusionados
    KB->>S: Semantic ranker
    KB-->>P: Fragmentos y referencias con URL del PDF
    P->>F: Fragmentos
    F-->>P: ProductAnswer con answer y sources
```

La ingesta, que ocurre una vez:

```mermaid
flowchart LR
    md["knowledge/docs/*.md"] -->|"publish.py: fpdf2"| pdf["8 PDF"]
    pdf -->|"Bearer Entra"| blob["Blob: contenedor productos"]
    blob -->|"indexer con identidad de AI Search"| sk["Skillset: SplitSkill 2000/200"]
    sk -->|"embeddings"| idx["Índice ks-productos-index<br/>11 fragmentos"]
    idx --> kb["Knowledge base kb-productos"]
```

---

## 9. Idempotencia de una escritura

```mermaid
sequenceDiagram
    autonumber
    participant H as agent-host
    participant M as mcp-server
    participant DB as Azure SQL

    H->>M: open_claim con motivo y X-Conversation-Id
    M->>M: key = sha256 de conversation_id, customer_id, tool y args
    M->>DB: BEGIN, INSERT idempotency_keys con resultado vacío
    M->>DB: INSERT reclamos
    M->>DB: UPDATE idempotency_keys con el resultado, COMMIT
    M-->>H: claim_id 7
    Note over H,M: Timeout de red, ResilientTools reintenta
    H->>M: open_claim igual
    M->>DB: INSERT idempotency_keys
    DB-->>M: IntegrityError por clave duplicada
    M->>DB: SELECT resultado_json
    M-->>H: claim_id 7, sin un segundo reclamo
```

Si la escritura falla, la transacción hace rollback y no queda clave guardada: un reintento vuelve a intentarlo (`test_failed_write_is_not_cached`).

---

## 10. Ejecución de evaluaciones y CI con OIDC

```mermaid
sequenceDiagram
    autonumber
    actor D as Desarrollador
    participant GH as GitHub Actions evals.yml
    participant OIDC as token.actions.githubusercontent.com
    participant EW as Entra workforce
    participant R as run_evals.py
    participant LM as MCP local con SQLite y seed
    participant F as Foundry gpt-5-mini y jueces
    participant IQ as Foundry IQ
    participant EV as Foundry Evaluations

    D->>GH: gh workflow run evals.yml -f limit=6
    GH->>GH: pytest unitarias y contrato, tabla en Job Summary
    GH->>OIDC: id-token con subject del repo y rama main
    OIDC-->>GH: JWT de GitHub
    GH->>EW: azure/login con client-id de banking-github-evals
    EW->>EW: Compara issuer, audience y subject con la credencial federada
    EW-->>GH: Token Entra sin secretos
    GH->>R: python -m evals.run_evals --name gh-RUN_ID
    R->>LM: Levanta python -m mcp_server con AUTH_MODE dev
    loop Cada caso
        R->>F: Workflow de triage en proceso
        R->>IQ: knowledge_base_retrieve
        R->>LM: Herramientas bancarias
        R->>R: TraceRecorder y expectations
    end
    R->>F: Jueces tool_call_accuracy, intent_resolution, task_adherence, relevance, groundedness
    R->>EV: evaluate con azure_ai_project
    R-->>GH: summary.md al Job Summary
```

| Pieza | Dónde |
|-------|-------|
| Permiso `id-token: write` | `.github/workflows/evals.yml` |
| Credencial federada | `azuread_application_federated_identity_credential.github_main` en `infra/ci.tf` |
| MCP local | `local_mcp()` en `run_evals.py` |
| Registro de herramientas | `TraceRecorder` |
| Jueces | `evaluate()` en `run_evals.py`, `is_reasoning_model=True`, `PF_WORKER_COUNT=2` |
| Resultados locales | `evals/results/<nombre>/rows.jsonl`, `evaluation.json`, `summary.md` |

---

## Modelo de datos

Esquema real de `mcp_server/db.py` (SQLAlchemy Core; igual en SQLite y Azure SQL).

```mermaid
erDiagram
    clientes ||--o{ cuentas : "tiene"
    clientes ||--o{ tarjetas : "tiene"
    cuentas ||--o{ tarjetas : "asocia"
    cuentas ||--o{ movimientos : "registra"
    clientes ||--o{ reclamos : "abre"
    clientes ||--o{ sesiones : "es dueño de"
    clientes |o--o{ auditoria : "genera"
    clientes ||--o{ tickets_escalamiento : "origina"

    clientes {
        int id PK
        nvarchar64 entra_oid UK "oid del token de External ID"
        nvarchar120 nombre
        nvarchar200 email
        datetime creado_en
    }
    cuentas {
        int id PK
        int cliente_id FK
        nvarchar32 numero UK
        nvarchar30 tipo
        numeric saldo "14,2"
        nvarchar3 moneda "PEN en el seed"
    }
    tarjetas {
        int id PK
        int cliente_id FK
        int cuenta_id FK
        nvarchar4 ultimos4
        nvarchar20 estado "activa o bloqueada"
        datetime bloqueada_en
    }
    movimientos {
        int id PK
        int cuenta_id FK
        datetime fecha
        nvarchar200 descripcion
        numeric monto
    }
    reclamos {
        int id PK
        int cliente_id FK
        nvarchar500 motivo
        nvarchar20 estado
        datetime creado_en
    }
    idempotency_keys {
        nvarchar64 idem_key PK "sha256 hex"
        nvarchar50 tool
        ntext resultado_json
        datetime creado_en
    }
    sesiones {
        nvarchar100 conversation_id PK
        int cliente_id FK
        ntext estado_json "checkpoint del workflow"
        datetime actualizado_en
    }
    auditoria {
        int id PK
        int cliente_id FK "nulo si fue rechazada antes de identificar"
        nvarchar50 tool
        ntext args_json "dígitos largos enmascarados"
        nvarchar20 resultado "ok, error o denegado"
        nvarchar100 conversation_id
        nvarchar32 trace_id
        datetime creado_en
    }
    tickets_escalamiento {
        int id PK
        int cliente_id FK
        nvarchar100 conversation_id
        nvarchar500 motivo "prefijo con la categoría"
        nvarchar20 estado
        datetime creado_en
    }
```

| Tabla | Quién escribe | Quién lee |
|-------|---------------|-----------|
| `clientes` | Seed; `create_test_users.py` actualiza `entra_oid` | MCP (`resolve_customer`), host (`customer_by_oid`) |
| `cuentas`, `tarjetas`, `movimientos`, `reclamos` | Seed; MCP (`block_card`, `open_claim`) | MCP |
| `idempotency_keys` | MCP (`_run_once`) | MCP |
| `sesiones` | Host (`SqlCheckpointStorage`) | Host |
| `auditoria` | MCP (`audit_middleware`) | Operadores |
| `tickets_escalamiento` | MCP (`escalate_to_human`) | Asesores (fuera de alcance) |

La columna `trace_id` de `auditoria` existe en el esquema, pero el middleware actual no la completa.
