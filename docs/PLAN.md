# Plan de trabajo

El proyecto avanza en 9 fases más una bonus. Cada fase entrega algo que funciona y se puede demostrar por sí solo. La infraestructura en Terraform crece por fase: cada una agrega solo lo que necesita, y todo `apply` se confirma antes de ejecutarse.

## Resumen

| Fase | Objetivo | Requiere Azure | Estado |
|------|----------|----------------|--------|
| [0](#fase-0-documentación-y-estructura) | Documentación y estructura | No | En curso |
| [1](#fase-1-core-bancario-simulado-y-servidor-mcp) | Core bancario simulado + servidor MCP | No | En curso |
| [2](#fase-2-agente-maf-mínimo) | Agente MAF mínimo | Sí (Foundry mínimo) | Hecha |
| [3](#fase-3-base-de-conocimiento) | Base de conocimiento | Sí | Pendiente |
| [4](#fase-4-orquestación) | Orquestación | Sí | Pendiente |
| [5](#fase-5-identidad-entra) | Identidad Entra | Sí | En curso |
| [6](#fase-6-producción) | Producción | Sí | Hecha (prueba de carga pendiente) |
| [7](#fase-7-a2a) | A2A | Sí | Pendiente |
| [8](#fase-8-evaluaciones-y-observabilidad-completas) | Evaluaciones y observabilidad completas | Sí | Pendiente |
| [Bonus](#fase-bonus) | Extensiones | Sí | Pendiente |

Reglas para todas las fases:

- [ ] Cero secretos en el repositorio.
- [ ] Sin nombres ni logos de bancos reales; sin datos económicos reales inventados.
- [ ] Datos 100 % sintéticos.
- [ ] `terraform destroy` al cerrar cada sesión de trabajo.

---

## Fase 0: Documentación y estructura

**Objetivo.** Dejar el diseño, las decisiones y el plan escritos antes de escribir código.

**Entregables.** `README.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/PLAN.md`, `.gitignore`.

**Criterios de aceptación.**

- [ ] El README se entiende en 2 minutos (qué es, qué demuestra, estado).
- [ ] Los diagramas Mermaid se renderizan en GitHub.
- [ ] Cada decisión relevante tiene un ADR con alternativas descartadas.
- [ ] Las piezas beta y preview están marcadas con fecha de verificación.

**Qué enseña.** Diseño de sistemas de agentes antes de implementar; documentación orientada a decisiones.

---

## Fase 1: Core bancario simulado y servidor MCP

**Objetivo.** Un servidor MCP local, seguro y probado, sin LLM ni Azure.

**Entregables.**

- Esquema SQLite local con las tablas del [modelo de datos](ARCHITECTURE.md#7-modelo-de-datos).
- Seed reproducible con Faker y semilla fija.
- 5 herramientas: `get_balance`, `list_transactions`, `get_claim_status`, `block_card`, `open_claim`.
- Idempotencia con `idempotency_key` y auditoría de cada llamada.
- Pruebas unitarias y de contrato.

**Criterios de aceptación.**

- [x] El seed genera siempre los mismos datos.
- [x] Ninguna herramienta acepta `customer_id` como argumento.
- [x] Un cliente no puede bloquear la tarjeta ni ver el reclamo de otro (test unitario).
- [x] Llamar `open_claim` dos veces con la misma clave crea un solo reclamo.
- [x] Cada llamada queda en `auditoria`.
- [ ] Las herramientas se listan e invocan desde MCP Inspector (cubierto por la prueba de contrato con el cliente MCP oficial; falta la verificación manual con el Inspector).
- [ ] CI ejecuta ruff y las pruebas en cada push (workflow listo; falta verlo correr en GitHub).

Guía: [fase-1-mcp.md](guides/fase-1-mcp.md).

**Qué enseña.** Diseño de herramientas MCP, autorización determinista, idempotencia con restricciones de base de datos.

---

## Fase 2: Agente MAF mínimo

**Objetivo.** Un agente Operaciones que conversa desde una CLI y usa el servidor MCP.

**Entregables.**

- Infraestructura mínima de Foundry: account, project y deployment de gpt-5-mini.
- Agente Operaciones con `FoundryChatClient` y `MCPStreamableHTTPTool`.
- CLI con un cliente de desarrollo fijo.
- `approval_mode` con `always_require` por defecto para escrituras.

**Criterios de aceptación.**

- [x] "¿Cuál es mi saldo?" devuelve el saldo del cliente de desarrollo (escenario `azure`, una ejecución).
- [x] `block_card` pide aprobación y solo se ejecuta al aprobar (escenario `azure` y prueba local determinista con modelo guionado).
- [x] Rechazar la aprobación no ejecuta la herramienta (sin fila en `auditoria` en la prueba local; tarjeta sigue activa en Azure).
- [x] La autenticación a Foundry usa credenciales Entra, sin API keys (`DefaultAzureCredential`; la cuenta tiene la autenticación local deshabilitada).

Guía: [fase-2-agente.md](guides/fase-2-agente.md). La estabilidad con N ejecuciones (umbral 4/5) se mide en la fase 4.

**Qué enseña.** Bucle de agente en MAF, cliente MCP, aprobaciones humanas.

---

## Fase 3: Base de conocimiento

**Objetivo.** Un agente Productos que responde con citas desde Foundry IQ.

**Entregables.**

- 6-8 documentos de productos ficticios en markdown, en `knowledge/`.
- Render a PDF y carga a Blob.
- AI Search Free y knowledge base de Foundry IQ creada por script Python.
- Agente Productos con `knowledge_base_retrieve` y salida estructurada de citas.
- Evaluaciones de recuperación.

**Criterios de aceptación.**

- [x] Cada respuesta de producto incluye al menos una cita válida (escenarios `-m azure`: 6 de 6 respuestas de producto con fuente válida).
- [x] Una pregunta sin fuente responde "no tengo esa información".
- [x] La misma pregunta 5 veces devuelve la misma cifra y la misma fuente.
- [x] Las evaluaciones de recuperación y de respuesta se reportan por separado (`evals/run_retrieval.py` y `tests/scenarios/test_productos_azure.py`).
- [x] Si agentic retrieval no está disponible en la región, se documenta y se usa el índice híbrido clásico (no aplicó: funciona en southcentralus sobre AI Search Free).

**Qué enseña.** RAG gestionado: ingesta, chunking, embeddings, búsqueda híbrida, recuperación con fuente y su evaluación.

---

## Fase 4: Orquestación

**Objetivo.** Triage enruta cada conversación al agente correcto con mínimo privilegio.

**Entregables.**

- Triage con `HandoffBuilder` hacia Productos y Operaciones.
- Herramienta `escalate_to_human(motivo)` que crea un ticket.
- Pruebas de escenario con modelo real y verificación de trayectoria vía function middleware.

**Criterios de aceptación.**

- [x] Preguntas de producto llegan a Productos; consultas personales, a Operaciones.
- [x] Fraude, fallecimiento del titular y reclamos legales escalan siempre.
- [x] Productos no tiene herramientas MCP bancarias (solo `escalate_to_human`, filtrada con `allowed_tools`); Operaciones no tiene acceso al conocimiento.
- [x] Cada escenario pasa al menos 4 de 5 ejecuciones (8 escenarios, todos 5/5 el 2026-11-06).

**Qué enseña.** Orquestación multiagente, mínimo privilegio, pruebas de trayectoria sobre modelos no deterministas.

---

## Fase 5: Identidad Entra

**Objetivo.** Reemplazar el cliente fijo por identidad real de extremo a extremo.

**Entregables.**

- Tenant de Entra External ID creado manualmente y documentado.
- App registrations con el provider `azuread`.
- Login por device code en la CLI.
- OBO del agente al MCP con certificado en Key Vault.
- Validación JWT en el servidor MCP y mapeo `oid` -> `customer_id`.
- Pruebas de red team de acceso cruzado entre clientes.

**Criterios de aceptación.**

- [ ] Un cliente solo ve sus propios datos.
- [ ] Un token con audiencia o emisor incorrectos es rechazado.
- [ ] Un prompt que intenta operar sobre otro cliente falla en el MCP aunque el LLM lo intente.
- [ ] No hay secretos de cliente: solo el certificado en Key Vault.

**Qué enseña.** Identidad de clientes con External ID, flujo on-behalf-of, validación de tokens.

**Estado (2026-11-06): en curso.** Hecho: app registrations, user flow, Key Vault con certificado, MCP `v3` en modo `entra`, clientes de prueba mapeados, pruebas unitarias y de contrato de acceso cruzado, y rechazos verificados contra el MCP desplegado (sin token, token basura, audiencia incorrecta, token sin `scp`: HTTP 401). Falta: correr la prueba interactiva `uv run pytest -m interactive -s -v` con device code y marcar los criterios. Guía: [fase-5-identidad.md](guides/fase-5-identidad.md).

---

## Fase 6: Producción

**Objetivo.** Desplegar en Azure con datos durables, resiliencia y guardrails.

**Entregables.**

- Azure SQL free offer con auth solo Entra (managed identity).
- Sesiones durables en la tabla `sesiones`.
- Capa de resiliencia: retry con backoff y jitter, timeouts.
- RAI policy con Indirect Attack detection habilitada.
- Container Apps con probes de liveness y readiness.
- Límite de longitud de entrada y rate limit por usuario.
- Prueba de carga con locust y prueba de caos.

**Criterios de aceptación.**

- [x] Matar una réplica a mitad de conversación no la interrumpe.
- [x] Una aprobación pendiente sobrevive al reinicio.
- [x] Un 429 simulado se reintenta con backoff.
- [x] Indirect Attack detection aparece habilitada en la RAI policy.
- [x] Ningún servicio usa connection strings con contraseña.
- [ ] La prueba de carga documenta latencia y tasa de errores (pendiente: requiere inicio de sesión interactivo, ver guía).

**Qué enseña.** Estado durable, resiliencia, guardrails de plataforma, operación en Container Apps.

**Estado (2026-11-08): hecha, con la prueba de carga pendiente.** Guía: [fase-6-produccion.md](guides/fase-6-produccion.md).

---

## Fase 7: A2A

**Objetivo.** Productos corre remoto y se comunica por A2A con trazas unificadas.

**Entregables.**

- Container App `productos-a2a` con `A2AExecutor` (experimental).
- Autenticación con managed identity.
- Instrumentación OTel de HTTPX y Starlette.
- Circuit breaker con fallback a `escalate_to_human`.
- Test de interoperabilidad con JSON-RPC crudo en `httpx`.

**Criterios de aceptación.**

- [x] `curl` a `/.well-known/agent-card.json` devuelve la card (desde dentro del entorno: el ingress es interno).
- [x] `SendMessage` directo funciona sin router (con token de managed identity; sin token responde 401).
- [x] El test de interoperabilidad pasa sin MAF ni cliente a2a-sdk.
- [ ] A2A Inspector conecta y conversa. Pendiente: verificación manual.
- [ ] Una conversación genera una sola traza distribuida en Application Insights. Pendiente: la propagación entre el host y Productos está verificada; falta la conversación por `/chat`, que requiere device code.
- [ ] Con Productos detenido, Triage escala a una persona. Pendiente: cubierto por prueba unitaria; falta repetirlo sobre el despliegue (requiere `/chat`).

**Qué enseña.** Protocolo A2A, autenticación entre servicios, propagación de contexto de traza, circuit breakers.

---

## Fase 8: Evaluaciones y observabilidad completas

**Objetivo.** Medir la calidad y la seguridad del agente con evidencia visible.

**Entregables.**

- Dataset de 20-30 casos, incluidos recuperación y consistencia.
- Ejecuciones de evaluación en Foundry.
- Red teaming con AI Red Teaming Agent y casos adversariales propios.
- Workflow `evals.yml` (`workflow_dispatch`) con Job Summary.
- Vista Agents de Application Insights y tracing de Foundry configurados.

**Criterios de aceptación.**

- [x] Las ejecuciones aparecen en Foundry > Evaluations con detalle por fila.
- [ ] Dos ejecuciones se pueden comparar en el portal. Pendiente: `cases-run1` y `cases-run2` existen con detalle por fila; falta abrir la vista Compare en el portal.
- [x] El Job Summary muestra tablas de pytest y evaluaciones.
- [x] La autenticación del workflow usa OIDC, sin secretos.
- [x] Los resultados del red team quedan documentados (casos adversariales propios; el escaneo del AI Red Teaming Agent queda pendiente por región, ver la [guía](guides/fase-8-evaluaciones.md)).

**Qué enseña.** Evaluación de agentes, red teaming, observabilidad de sistemas con LLM.

---

## Fase bonus

| Extensión | Qué aporta |
|-----------|------------|
| Hosted agents en Foundry | Comparar hosting gestionado (beta) contra Container Apps |
| Pipeline RAG manual | Comparar contra Foundry IQ en calidad, costo y control |
| UI web | Interfaz para no técnicos |
| Doble OBO para Operaciones remoto | Operaciones vía A2A con identidad del cliente |
