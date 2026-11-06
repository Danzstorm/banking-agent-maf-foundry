# Decisiones de arquitectura

Registro de decisiones (ADR) del proyecto. Todas están aceptadas y verificadas al 2026-11-06. Cada entrada explica qué se decidió, por qué y qué se descartó, con foco en seguridad y operación en producción.

## Índice

| ADR | Tema | Área |
|-----|------|------|
| [001](#adr-001-maf-como-runtime-del-agente-y-foundry-como-plataforma) | MAF como runtime del agente y Foundry como plataforma | Plataforma |
| [002](#adr-002-gpt-5-mini-en-global-standard) | gpt-5-mini en Global Standard | Plataforma |
| [003](#adr-003-separación-estricta-de-fuentes-de-verdad) | Separación estricta de fuentes de verdad | Datos |
| [004](#adr-004-orquestación-con-handoffbuilder-y-mínimo-privilegio) | Orquestación con HandoffBuilder y mínimo privilegio | Agentes |
| [005](#adr-005-servidor-mcp-propio-con-identidad-derivada-del-token) | Servidor MCP propio con identidad derivada del token | Seguridad |
| [006](#adr-006-aprobación-humana-obligatoria-para-escrituras) | Aprobación humana obligatoria para escrituras | Seguridad |
| [007](#adr-007-idempotencia-con-idempotency_key) | Idempotencia con `idempotency_key` | Confiabilidad |
| [008](#adr-008-managed-identity-entre-servicios-y-entra-external-id-para-clientes) | Managed identity entre servicios y Entra External ID para clientes | Identidad |
| [009](#adr-009-obo-con-certificado-en-key-vault) | OBO con certificado en Key Vault | Identidad |
| [010](#adr-010-validación-jwt-en-el-código-del-servidor-mcp) | Validación JWT en el código del servidor MCP | Identidad |
| [011](#adr-011-azure-sql-database-free-offer) | Azure SQL Database free offer | Datos |
| [012](#adr-012-foundry-iq-sobre-ai-search-free) | Foundry IQ sobre AI Search Free | Conocimiento |
| [013](#adr-013-sesiones-durables-en-azure-sql) | Sesiones durables en Azure SQL | Confiabilidad |
| [014](#adr-014-capa-de-resiliencia-propia) | Capa de resiliencia propia | Confiabilidad |
| [015](#adr-015-agente-productos-remoto-vía-a2a) | Agente Productos remoto vía A2A | Agentes |
| [016](#adr-016-propagación-manual-de-trazas-en-a2a) | Propagación manual de trazas en A2A | Observabilidad |
| [017](#adr-017-guardrails-de-contenido-con-indirect-attack-habilitado) | Guardrails de contenido con Indirect Attack habilitado | Seguridad |
| [018](#adr-018-respuestas-consistentes-y-fundamentadas) | Respuestas consistentes y fundamentadas | Conocimiento |
| [019](#adr-019-pirámide-de-pruebas-de-6-capas) | Pirámide de pruebas de 6 capas | Calidad |
| [020](#adr-020-ci-con-oidc-y-evaluaciones-bajo-demanda) | CI con OIDC y evaluaciones bajo demanda | Calidad |
| [021](#adr-021-terraform-incremental-con-control-de-costos) | Terraform incremental con control de costos | Infraestructura |
| [022](#adr-022-hosted-agents-fuera-del-núcleo) | Hosted agents fuera del núcleo | Plataforma |

---

## ADR-001: MAF como runtime del agente y Foundry como plataforma

**Estado:** Aceptada

**Contexto.** Un agente bancario necesita control fino sobre herramientas, aprobaciones, sesiones y resiliencia. Foundry ofrece tanto agentes server-side (Foundry Agent Service) como acceso directo al modelo.

**Decisión.** El bucle del agente corre en nuestro código con Microsoft Agent Framework (`agent-framework` 1.24, GA desde 1.0 el 2026-04-03) usando `FoundryChatClient` sobre la Responses API. MAF es dueño de herramientas, cliente MCP, aprobaciones, orquestación, A2A y sesiones. Foundry aporta modelo, conocimiento (Foundry IQ), tracing y evaluaciones.

**Consecuencias.**

- (+) Control total del flujo: middleware, aprobaciones, persistencia y retries son código testeable.
- (+) El agente es portable entre hosts (Container Apps hoy, hosted agents mañana).
- (-) Somos responsables de la resiliencia y del estado; Foundry no los gestiona por nosotros.

**Alternativas descartadas.**

- Agentes server-side de Foundry Agent Service: menos control sobre el bucle y las aprobaciones, y más difícil de probar localmente.

---

## ADR-002: gpt-5-mini en Global Standard

**Estado:** Aceptada

**Contexto.** El agente necesita buen uso de herramientas a bajo costo, y el proyecto debe costar casi cero en reposo.

**Decisión.** Se usa gpt-5-mini con despliegue Global Standard (pago por token). Como modelo de razonamiento no admite `temperature`; se fija `reasoning_effort` explícitamente.

**Consecuencias.**

- (+) Costo aproximado de USD 0,25 entrada / 2,00 salida por 1M tokens (fuente de terceros; se verifica en la página oficial de precios).
- (+) Sin costo fijo cuando no hay tráfico.
- (-) Global Standard puede procesar fuera de la región; aceptable para datos sintéticos, a revisar para datos reales.
- (-) La disponibilidad condiciona la región; se verifica en la fase de infraestructura.

**Alternativas descartadas.**

- Provisioned throughput: costo fijo injustificable para un portafolio.
- Modelos más grandes: mayor costo sin ganancia demostrada en este dominio acotado.

---

## ADR-003: Separación estricta de fuentes de verdad

**Estado:** Aceptada

**Contexto.** Un RAG puede devolver cifras plausibles pero equivocadas, y una base transaccional no es el lugar para reglas de producto.

**Decisión.** Los saldos y movimientos **nunca** salen del RAG; las comisiones y reglas de producto **nunca** salen de SQL. Los datos del cliente vienen solo de herramientas MCP; el conocimiento de productos, solo de Foundry IQ.

**Consecuencias.**

- (+) Cada respuesta tiene una única fuente auditable.
- (+) Elimina una clase completa de alucinaciones sobre dinero.
- (-) Preguntas mixtas ("¿cuánto me cobraron de comisión este mes?") requieren combinar ambos agentes.

**Alternativas descartadas.**

- Indexar datos de clientes en el buscador: fuga de datos personales y riesgo de desactualización.

---

## ADR-004: Orquestación con HandoffBuilder y mínimo privilegio

**Estado:** Aceptada

**Contexto.** Un único agente con todas las herramientas amplía la superficie de ataque: una inyección en un documento podría disparar una operación.

**Decisión.** Se usa `HandoffBuilder` (`agent_framework.orchestrations`, estable). Triage solo enruta. Productos solo accede al conocimiento. Operaciones solo accede a herramientas MCP. Todos pueden llamar a `escalate_to_human(motivo)`, que crea un ticket. Fraude, fallecimiento del titular y reclamos legales siempre escalan.

**Consecuencias.**

- (+) Un documento manipulado no puede llegar a una herramienta de escritura.
- (+) Instrucciones más cortas y evaluables por agente.
- (-) Más turnos de modelo por conversación (latencia y costo).

**Alternativas descartadas.**

- Agente único con todas las herramientas: viola mínimo privilegio.
- Orquestación con un planner o group chat: más impredecible y difícil de evaluar.

---

## ADR-005: Servidor MCP propio con identidad derivada del token

**Estado:** Aceptada

**Contexto.** Si el LLM decide sobre qué cliente opera, una inyección de prompt basta para acceder a datos ajenos.

**Decisión.** Servidor MCP propio en Python sobre streamable HTTP con `get_balance()`, `list_transactions(from, to)`, `get_claim_status(claim_id)`, `block_card(card_id)` y `open_claim(motivo)`. Ninguna herramienta recibe `customer_id`: el servidor lo obtiene del token Entra validado (`oid` -> `customer_id`). Las herramientas que reciben un id validan pertenencia. Toda entrada se valida con pydantic y cada llamada se audita. En MAF se usa `MCPStreamableHTTPTool` con `header_provider` para inyectar el `Authorization` por ejecución.

**Consecuencias.**

- (+) Aunque el LLM sea engañado, el servidor rechaza el acceso cruzado. Se prueba con tests adversariales.
- (+) La autorización vive en código determinista y testeable sin LLM.
- (-) Mantener un servidor adicional.

**Alternativas descartadas.**

- Pasar `customer_id` como argumento de herramienta: confía en el LLM para la autorización.
- Herramientas locales en el agente host: mezcla autorización con orquestación y no es reutilizable por otros clientes MCP.

---

## ADR-006: Aprobación humana obligatoria para escrituras

**Estado:** Aceptada

**Contexto.** Bloquear una tarjeta o abrir un reclamo tiene efectos reales para el cliente.

**Decisión.** `block_card` y `open_claim` requieren aprobación explícita. En MAF se configura `approval_mode` como diccionario con `always_require` por defecto para herramientas de escritura; solo las lecturas se marcan explícitamente como `never_require`.

**Consecuencias.**

- (+) Ninguna escritura ocurre sin consentimiento del cliente.
- (+) Una herramienta nueva queda protegida por defecto.
- (-) Un paso extra en la conversación.
- (-) La aprobación pendiente debe sobrevivir a reinicios (ver [ADR-013](#adr-013-sesiones-durables-en-azure-sql)).

**Alternativas descartadas.**

- Confirmación pedida por el propio LLM en texto: no es un control, el modelo puede omitirla.

---

## ADR-007: Idempotencia con idempotency_key

**Estado:** Aceptada

**Contexto.** Los reintentos por timeout o red pueden ejecutar dos veces una escritura y crear reclamos duplicados.

**Decisión.** Las herramientas de escritura usan `idempotency_key = sha256(conversation_id + customer_id + tool + args normalizados)`, guardada en la tabla `idempotency_keys` con restricción `UNIQUE`. Una clave repetida devuelve el resultado original sin reejecutar. `block_card` además es idempotente por naturaleza.

**Consecuencias.**

- (+) Los reintentos son seguros; la base de datos garantiza la unicidad, no el código.
- (+) Test de aceptación directo: llamar dos veces produce un solo reclamo.
- (-) Dos reclamos legítimos idénticos en la misma conversación se consideran el mismo; aceptable y documentado.

**Alternativas descartadas.**

- Deduplicación en memoria: no funciona con varias réplicas.
- Clave generada por el LLM: no determinista.

---

## ADR-008: Managed identity entre servicios y Entra External ID para clientes

**Estado:** Aceptada

**Contexto.** Los secretos de conexión son la causa más común de filtraciones. Los clientes de un banco no son usuarios del tenant corporativo.

**Decisión.** Toda comunicación servicio a servicio (agente a Foundry, agente a SQL, MCP a SQL, agente a Key Vault, agente a Productos) usa managed identity. Los clientes se autentican en un tenant de Entra External ID. El tenant se crea manualmente una vez y queda documentado, porque la API ARM `ciamDirectories` solo existe en preview. Las app registrations se gestionan con el provider `azuread` apuntando al tenant externo. La CLI usa device code, soportado en External ID. La identidad se incorpora en su propia fase, después de que el agente funcione con un cliente fijo de desarrollo.

**Consecuencias.**

- (+) Cero secretos de conexión en el código y en la configuración.
- (+) Separación clara entre identidad corporativa e identidad de clientes.
- (-) Un paso manual fuera de Terraform.

**Alternativas descartadas.**

- Azure AD B2C: producto reemplazado por External ID para nuevos desarrollos.
- Usuarios en el tenant workforce: mezcla empleados y clientes.
- Connection strings con usuario y contraseña: secreto innecesario.

---

## ADR-009: OBO con certificado en Key Vault

**Estado:** Aceptada

**Contexto.** El servidor MCP necesita la identidad del cliente final, no la del agente. La federated credential de managed identity no puede cruzar del tenant workforce al tenant externo.

**Decisión.** El agente host intercambia el token del cliente por uno para la API MCP mediante on-behalf-of, autenticándose con un certificado guardado en Key Vault y leído con managed identity.

**Consecuencias.**

- (+) El MCP recibe un token con la identidad real del cliente.
- (+) El certificado nunca sale de Key Vault hacia el repositorio.
- (-) Hay que rotar el certificado; se documenta el procedimiento.

**Alternativas descartadas.**

- Client secret: más fácil de filtrar y con rotación igual de necesaria.
- Federated credential de managed identity: no soportada entre estos tenants.
- Reenviar el token del cliente tal cual: audiencia incorrecta y mala práctica.

---

## ADR-010: Validación JWT en el código del servidor MCP

**Estado:** Aceptada

**Contexto.** Container Apps ofrece autenticación integrada (Easy Auth), pero puede interferir con el descubrimiento del protocolo MCP.

**Decisión.** El servidor MCP valida el JWT en su propio código: firma, emisor, audiencia y expiración, y luego mapea `oid` a `customer_id`.

**Consecuencias.**

- (+) Control explícito y testeable de la autorización.
- (+) El descubrimiento MCP funciona sin excepciones de ruta.
- (-) Código de seguridad propio que debe tener tests dedicados.

**Alternativas descartadas.**

- Easy Auth de Container Apps: riesgo de romper el descubrimiento MCP y menos visibilidad en las pruebas.

---

## ADR-011: Azure SQL Database free offer

**Estado:** Aceptada

**Contexto.** Se necesita una base relacional compartida entre réplicas, con auth sin secretos y costo cero en reposo.

**Decisión.** Azure SQL Database con free offer (serverless, 100 000 vCore-s/mes, 32 GB, AutoPause al llegar al límite para que nunca facture). Se habilita con `azapi` (`useFreeLimit`), porque `azurerm` no lo expone. Autenticación solo Entra con managed identity (`mssql-python`, `ActiveDirectoryMSI`). Las pruebas unitarias usan SQLite local.

**Consecuencias.**

- (+) Costo cero garantizado y sin contraseñas.
- (+) Datos compartidos entre réplicas y persistentes.
- (-) Auto-pausa tras 15 min de inactividad: la primera consulta es lenta. Se precalienta antes de una demo.
- (-) Diferencias de dialecto entre SQLite y Azure SQL; se cubren con pruebas de integración.

**Alternativas descartadas.**

- SQLite en Azure: el filesystem del contenedor es efímero y no se comparte entre réplicas.
- Cosmos DB: no aporta ventajas para un modelo relacional con restricciones `UNIQUE`.

---

## ADR-012: Foundry IQ sobre AI Search Free

**Estado:** Aceptada

**Contexto.** El conocimiento de productos necesita ingesta, chunking, embeddings, búsqueda híbrida y recuperación con fuente.

**Decisión.** Base de conocimiento de Foundry IQ (GA desde abril 2026, recuperación extractiva) sobre Azure AI Search Free. Las fuentes son 6-8 documentos ficticios en markdown versionados en el repo, renderizados a PDF y cargados a Blob. Se consume vía el endpoint MCP de la KB (`knowledge_base_retrieve`). La síntesis de respuestas de Foundry IQ está en preview, por lo que la hace nuestro agente. Knowledge source y KB son objetos de data plane: los crea un script Python, no Terraform. El pipeline manual queda como fase bonus.

**Consecuencias.**

- (+) RAG gestionado con costo cero y fuentes auditables.
- (+) Evaluaciones separadas de recuperación y de respuesta.
- (-) Agentic retrieval en Free solo existe en regiones seleccionadas; el fallback es un índice híbrido clásico.
- (-) Menos control sobre el chunking que un pipeline propio.

**Alternativas descartadas.**

- Pipeline propio desde el inicio: más código sin valor diferencial en el núcleo.
- Gestionar objetos de data plane con Terraform: soporte inmaduro y acoplamiento con el ciclo de infraestructura.

---

## ADR-013: Sesiones durables en Azure SQL

**Estado:** Aceptada

**Contexto.** Con varias réplicas y scale-to-zero, una conversación no puede depender de la memoria de un proceso.

**Decisión.** Se persiste `session.to_dict()` como JSON por `conversation_id` en la tabla `sesiones`, incluidas las aprobaciones pendientes. Cualquier réplica retoma la conversación.

**Consecuencias.**

- (+) Reinicios y escalado no pierden conversaciones ni aprobaciones.
- (-) No existe un sample oficial de reanudación tras reinicio; se demuestra con una prueba de caos.

**Alternativas descartadas.**

- Afinidad de sesión (sticky sessions): falla al reiniciar la réplica.
- Redis: un servicio más, con costo en reposo.

---

## ADR-014: Capa de resiliencia propia

**Estado:** Aceptada

**Contexto.** MAF no trae retry ni circuit breaker; el SDK de OpenAI solo hace 2 reintentos por defecto.

**Decisión.** Retry propio con backoff exponencial y jitter ante 429/5xx para modelo, MCP y A2A. Timeouts por llamada (MCP: `request_timeout` de 30 s con reconexión automática). Circuit breaker hacia A2A. Probes de liveness y readiness en Container Apps. Mínimo 0 réplicas en desarrollo y 1 el día de la demo. Prueba de carga pequeña con locust y prueba de caos documentada.

**Consecuencias.**

- (+) Comportamiento predecible ante fallas, probado y medido.
- (-) Código de infraestructura propio que mantener.
- (-) Retry solo es seguro sobre escrituras porque existe [ADR-007](#adr-007-idempotencia-con-idempotency_key).

**Alternativas descartadas.**

- Confiar en los reintentos del SDK: cobertura incompleta y sin circuit breaker.
- Service mesh: desproporcionado para tres servicios.

---

## ADR-015: Agente Productos remoto vía A2A

**Estado:** Aceptada (componente experimental)

**Contexto.** Se quiere demostrar interoperabilidad entre agentes sin complicar la cadena de identidad.

**Decisión.** Productos corre como Container App separada, expuesta como servidor A2A nativo con `agent_framework.a2a.A2AExecutor` y `a2a-sdk`. Se usa A2A 1.0: card en `/.well-known/agent-card.json`, JSON-RPC con método `SendMessage` y header `A2A-Version: 1.0`. La autenticación servicio a servicio usa token Entra de managed identity. Se elige Productos porque no necesita identidad del cliente final. El paquete `agent-framework-a2a` está en beta, por lo que el componente se marca experimental.

**Consecuencias.**

- (+) Interoperabilidad demostrable con clientes que no son MAF.
- (+) Se evita una doble cadena OBO.
- (-) Dependencia de un paquete beta.
- (-) Un salto de red más; mitigado con circuit breaker y escalamiento.

**Alternativas descartadas.**

- Operaciones remoto: requiere doble OBO; queda como paso siguiente (fase bonus).
- Todo local: no demuestra A2A.

---

## ADR-016: Propagación manual de trazas en A2A

**Estado:** Aceptada

**Contexto.** MAF inyecta `traceparent` automáticamente solo en llamadas MCP. En A2A la traza se cortaría en dos.

**Decisión.** Instrumentación manual con OpenTelemetry del cliente HTTPX en el agente host y del servidor Starlette en Productos. Criterio de aceptación: una sola traza distribuida en Application Insights.

**Consecuencias.**

- (+) Diagnóstico de punta a punta de una conversación.
- (-) Configuración adicional que validar en cada actualización de dependencias.

**Alternativas descartadas.**

- Correlación por ids en logs: no ofrece la vista de árbol ni latencias por tramo.

---

## ADR-017: Guardrails de contenido con Indirect Attack habilitado

**Estado:** Aceptada

**Contexto.** Los filtros DefaultV2 y la detección de jailbreak de Foundry vienen activos, pero la detección de Indirect Attack viene desactivada. Spotlighting solo aplica a Chat Completions, y usamos Responses.

**Decisión.** Se mantienen DefaultV2 y jailbreak detection, y se habilita explícitamente Indirect Attack detection con `azurerm_cognitive_account_rai_policy`. La falta de spotlighting se cubre con instrucciones del agente y evaluaciones de red team. El agente host aplica límite de longitud de entrada y rate limit por usuario.

**Consecuencias.**

- (+) Defensa en capas: plataforma, instrucciones, arquitectura (ADR-004, ADR-005) y pruebas.
- (-) Posibles falsos positivos; se monitorean en las evaluaciones.

**Alternativas descartadas.**

- Depender solo de los valores por defecto: deja abierta la inyección indirecta.
- Usar Chat Completions para tener spotlighting: perdemos la Responses API que usa `FoundryChatClient`.

---

## ADR-018: Respuestas consistentes y fundamentadas

**Estado:** Aceptada

**Contexto.** Un cliente que pregunta dos veces lo mismo debe recibir la misma cifra y la misma fuente. Idempotencia (escrituras) y consistencia (respuestas) son problemas distintos.

**Decisión.** Las respuestas de productos se basan solo en fuentes recuperadas, con citas; si no hay fuente, se responde "no tengo esa información" o se escala. Se fija `reasoning_effort` y se usa salida estructurada para las citas. Se evalúa la consistencia con la misma pregunta 5 veces, comparando cifra y fuente. Solo se cachea la recuperación de conocimiento (no personal, con TTL), nunca respuestas finales con datos del cliente.

**Consecuencias.**

- (+) Respuestas verificables y medibles.
- (+) La caché no puede filtrar datos de un cliente a otro.
- (-) Más respuestas de "no tengo esa información" cuando la base es incompleta; es el comportamiento deseado.

**Alternativas descartadas.**

- Bajar `temperature`: no aplica a modelos de razonamiento.
- Cachear respuestas completas: riesgo de fuga de datos personales.

---

## ADR-019: Pirámide de pruebas de 6 capas

**Estado:** Aceptada

**Contexto.** Un agente combina lógica determinista con un modelo no determinista. Ninguna técnica sola cubre ambos.

**Decisión.**

| Capa | Contenido |
|------|-----------|
| 1. Unitarias | pytest sin LLM, incluida autorización (no puedes bloquear la tarjeta de otro cliente) |
| 2. Contrato MCP | MCP Inspector + cliente MCP en pytest |
| 3. Escenarios | Modelo real; trayectoria verificada con function middleware de MAF (`FunctionInvocationContext`); N ejecuciones con umbral (ej. 4/5) |
| 4. Evaluaciones | `azure-ai-evaluation` 1.18 sobre 20-30 casos: `tool_call_accuracy` y `groundedness` (GA), `intent_resolution` y `task_adherence` (preview), recuperación y consistencia |
| 5. Red teaming | Casos adversariales propios en pytest que prueban que el MCP rechaza acceso cruzado aunque el LLM sea engañado, más AI Red Teaming Agent (preview, `RedTeam().scan(target=callback)`, single-turn) |
| 6. Trazas | OTel a Application Insights (vista Agents, preview) y tracing de Foundry |

**Consecuencias.**

- (+) Las capas baratas atrapan la mayoría de los errores; las caras validan el comportamiento del modelo.
- (+) La seguridad crítica se prueba sin depender del modelo.
- (-) Las capas 3 a 5 consumen tokens y requieren infraestructura activa.

**Alternativas descartadas.**

- Solo evaluaciones con LLM: lentas, caras y no prueban la autorización.
- Asserts exactos sobre texto del modelo: frágiles.

---

## ADR-020: CI con OIDC y evaluaciones bajo demanda

**Estado:** Aceptada

**Contexto.** Un workflow programado que depende de infraestructura destruida falla todos los días y genera ruido.

**Decisión.** GitHub Actions ejecuta lint (ruff) y pruebas unitarias y de contrato en cada push, sin Azure. Escenarios, evaluaciones y red team corren solo con `workflow_dispatch`, sin cron. La autenticación a Azure usa exclusivamente OIDC, sin secretos. Los resultados se publican en el Job Summary como tabla markdown.

**Consecuencias.**

- (+) CI rápido, gratuito y sin secretos.
- (+) Las evaluaciones corren cuando la infraestructura existe.
- (-) Las regresiones del modelo no se detectan automáticamente; se ejecutan antes de cada demo.

**Alternativas descartadas.**

- Evaluaciones en cron: fallan con infraestructura destruida.
- Service principal con secreto: secreto innecesario.

---

## ADR-021: Terraform incremental con control de costos

**Estado:** Aceptada

**Contexto.** Desplegar toda la infraestructura desde el inicio aumenta costos y depuración. El proyecto debe costar casi cero.

**Decisión.** Terraform con `azurerm` para el núcleo (`azurerm_cognitive_account` kind AIServices, `azurerm_cognitive_account_project`, `azurerm_cognitive_deployment`), `azapi` donde `azurerm` no llega (SQL free offer) y `azuread` para las app registrations de External ID. La infraestructura crece por fase: cada fase agrega solo lo que necesita, y todo `apply` se confirma antes de ejecutarse. Alerta de presupuesto de USD 20 y `terraform destroy` al final de cada sesión de trabajo.

**Consecuencias.**

- (+) Costo en reposo cercano a cero.
- (+) Cada fase es reproducible desde cero.
- (-) Recrear la infraestructura toma tiempo al iniciar cada sesión.

**Alternativas descartadas.**

- Bicep: válido, pero Terraform permite gestionar el tenant externo con `azuread` en el mismo flujo.
- Infraestructura permanente: costo innecesario.

---

## ADR-022: Hosted agents fuera del núcleo

**Estado:** Aceptada

**Contexto.** Foundry permite hospedar agentes MAF, pero el paquete de hosting está en beta.

**Decisión.** El núcleo se despliega en Container Apps. Los hosted agents en Foundry quedan como fase bonus.

**Consecuencias.**

- (+) El núcleo no depende de piezas beta en el camino crítico de despliegue.
- (-) No se demuestra el hosting gestionado hasta la fase bonus.

**Alternativas descartadas.**

- Hosted agents desde el inicio: dependencia beta en el despliegue principal.
