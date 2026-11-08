# 09. Glosario

[Índice de la guía](README.md) · Anterior: [08. Operación](08-operacion.md)

Términos en orden alfabético. Entre paréntesis, dónde aparecen en el proyecto.

| Término | Definición |
|---------|------------|
| **A2A (Agent2Agent)** | Protocolo abierto para que agentes se comuniquen entre sí. Versión 1.0, JSON-RPC (`productos_a2a.py`). |
| **`A2AAgent`** | Cliente A2A de MAF; aquí se expone como herramienta con `as_tool()` (`productos.py`). |
| **`A2AExecutor`** | Adaptador de MAF que ejecuta un agente detrás de un servidor A2A (`productos_a2a.py`). |
| **`A2A-Version`** | Header obligatorio con la versión del protocolo; sin él se asume 0.3 y hay error `-32009`. |
| **Access token** | Token que autoriza llamar a una API. En Entra es un JWT. |
| **ACR (Azure Container Registry)** | Registro privado de imágenes (`acrbankingagentke9ih`). |
| **AcrPull** | Rol que permite descargar imágenes del ACR. |
| **ADR (Architecture Decision Record)** | Registro de una decisión con contexto, alternativas y consecuencias (`docs/DECISIONS.md`). |
| **Agent card** | Documento JSON público que describe un agente A2A (`/.well-known/agent-card.json`). |
| **Agente** | Modelo + instrucciones + herramientas + bucle de ejecución. |
| **Agentic retrieval** | Recuperación de AI Search en la que un modelo puede planificar consultas; aquí con esfuerzo `minimal`. |
| **AI Red Teaming Agent** | Servicio de Foundry (preview) que genera ataques y los puntúa (`run_redteam.py`). |
| **AIServices** | Kind de la cuenta de Cognitive Services que corresponde a Foundry. |
| **App registration** | Definición de una aplicación en Entra: client id, scopes, roles, credenciales. |
| **App role** | Permiso que una API concede a aplicaciones; aparece en el claim `roles` (`Productos.Invoke`). |
| **Application Insights** | Servicio de APM de Azure Monitor (`appi-banking-agent-ke9ih`). |
| **`approval_mode`** | Configuración de MAF que exige aprobación humana antes de ejecutar herramientas. |
| **Audiencia (`aud`)** | Claim del token que indica para qué API fue emitido. |
| **Auditoría** | Tabla `auditoria` con cada `tools/call`. |
| **AutoPause** | Pausa automática de Azure SQL serverless; también el comportamiento al agotar el free offer. |
| **`azapi`** | Provider de Terraform que habla directo con la API ARM; usado donde `azurerm` no llega. |
| **`azuread`** | Provider de Terraform para Entra ID; alias `azuread.external` para el tenant de clientes. |
| **`azurerm`** | Provider principal de Terraform para Azure. |
| **Backoff exponencial** | Esperas crecientes entre reintentos (1 s, 2 s, 4 s...). |
| **BM25** | Algoritmo clásico de búsqueda por palabras clave. |
| **`BREAKER_FALLBACK`** | Mensaje que recibe el modelo cuando el circuito está abierto (`resilience.py`). |
| **Checkpoint** | Estado serializado de un workflow en un superstep; aquí se guarda en `sesiones`. |
| **Chunking** | Partir documentos en fragmentos para indexarlos. |
| **CIAM** | Customer Identity and Access Management: identidad de clientes (External ID). |
| **Circuit breaker** | Patrón que deja de llamar a una dependencia que falla repetidamente. |
| **Claim** | Campo dentro de un JWT (`oid`, `aud`, `scp`...). |
| **Client id** | Identificador público de una app registration o de una managed identity. |
| **Cliente confidencial** | App que puede guardar una credencial (certificado); aquí `banking-agent-api`. |
| **Cliente público** | App que no puede guardar secretos (CLI); aquí `banking-cli`. |
| **Consentimiento de administrador** | Aprobación previa de permisos para todos los usuarios (`delegated_permission_grant`). |
| **Container App** | Aplicación en Azure Container Apps. |
| **Container Apps environment** | Límite de red y logs que comparten varias Container Apps (`cae-banking-agent-ke9ih`). |
| **Credencial federada** | Confianza de una identidad de Entra en tokens de otro emisor (GitHub OIDC). |
| **`customer_id`** | Id interno del cliente; nunca lo envía el modelo, sale del token. |
| **`DefaultAzureCredential`** | Credencial de `azure-identity` que prueba `az login`, managed identity, OIDC, etc. |
| **DefaultV2** | Política de contenido base de Foundry. |
| **Device code** | Flujo OAuth para dispositivos sin navegador: código más URL de verificación. |
| **Easy Auth** | Autenticación integrada de Container Apps; no se usa (ADR-010). |
| **Embedding** | Vector numérico que representa el significado de un texto. |
| **Entra ID** | Servicio de identidad de Microsoft (antes Azure AD). |
| **`entra_oid`** | Columna de `clientes` con el `oid` del usuario de External ID. |
| **`escalate_to_human`** | Herramienta MCP que crea un ticket para un asesor. |
| **Evaluador / juez** | Modelo que califica respuestas con una rúbrica (`azure-ai-evaluation`). |
| **External ID** | Producto de Entra para identidad de clientes; reemplaza a Azure AD B2C. |
| **Extractiva (recuperación)** | Devuelve fragmentos sin redactar una respuesta. |
| **Foundry (Microsoft Foundry)** | Plataforma de IA de Azure: modelos, proyectos, evaluaciones, tracing. |
| **Foundry IQ** | Capa de conocimiento de Foundry sobre AI Search. |
| **Foundry User** | Rol de plano de datos sobre la cuenta y proyectos de Foundry (antes Azure AI User). |
| **`FoundryChatClient`** | Cliente de chat de MAF hacia la Responses API de un proyecto de Foundry. |
| **Free offer (Azure SQL)** | Base serverless gratuita con límite mensual de vCore-segundos. |
| **Function middleware** | Middleware de MAF que envuelve cada llamada a herramienta. |
| **`function_invocation_kwargs`** | Contexto del host que MAF entrega a las herramientas; el modelo no lo ve. |
| **GlobalStandard** | Tipo de despliegue de pago por token con procesamiento global. |
| **Groundedness** | Métrica: la respuesta se apoya en el contexto entregado. |
| **Guardrails** | Controles de contenido y comportamiento (RAI policy, instrucciones, límites). |
| **Half-open (semiabierto)** | Estado del breaker que deja pasar una llamada de prueba. |
| **Handoff** | Transferencia de la conversación de un agente a otro (`HandoffBuilder`). |
| **`HandoffAgentUserRequest`** | Solicitud del workflow que espera el siguiente mensaje del cliente. |
| **`header_provider`** | Función de `MCPStreamableHTTPTool` que genera los headers de cada request. |
| **hit@1 / hit@k** | Métricas de recuperación: el documento correcto está primero o entre los k primeros. |
| **Idempotencia** | Ejecutar dos veces equivale a ejecutar una vez (`idempotency_keys`). |
| **Indirect Attack** | Prompt Shield que detecta instrucciones ocultas en documentos. |
| **Ingress externo / interno** | Exposición pública o solo dentro del environment. |
| **Issuer (`iss`)** | Emisor del token. |
| **Jailbreak** | Intento directo de que el modelo ignore sus reglas; también el Prompt Shield que lo detecta. |
| **Jitter** | Azar agregado a las esperas de reintento. |
| **JSON-RPC** | Protocolo de llamada remota en JSON; lo usan MCP y A2A. |
| **JWKS** | Conjunto de claves públicas con las que se verifican firmas de JWT. |
| **JWT** | JSON Web Token: encabezado, claims y firma. |
| **KEDA** | Escalador por eventos que usa Container Apps por debajo. |
| **Key Vault** | Almacén de secretos, claves y certificados (`kv-banking-ke9ih`). |
| **`kid`** | Id de la clave que firmó un JWT. |
| **Knowledge base** | Objeto de Foundry IQ que agrupa fuentes y expone recuperación (`kb-productos`). |
| **Knowledge source** | Objeto de Foundry IQ que apunta a los datos (`ks-productos`). |
| **KQL** | Lenguaje de consulta de Log Analytics. |
| **Liveness / readiness** | Probes: ¿el proceso vive? / ¿puede recibir tráfico? |
| **Log Analytics** | Almacén de logs de Azure Monitor (`log-banking-agent-ke9ih`). |
| **MAF (Microsoft Agent Framework)** | SDK de Microsoft para agentes; aquí `agent-framework-core` 1.24. |
| **Managed identity** | Identidad gestionada por Azure para un recurso, sin secretos. |
| **MCP (Model Context Protocol)** | Protocolo abierto para exponer herramientas a clientes de IA. |
| **MCP Inspector** | Herramienta web para probar servidores MCP. |
| **`MCPStreamableHTTPTool`** | Cliente MCP de MAF sobre streamable HTTP. |
| **Mínimo privilegio** | Dar a cada componente solo los permisos que necesita. |
| **MSAL** | Librería de Microsoft para obtener tokens (`identity.py`). |
| **OBO (on-behalf-of)** | Intercambio de un token de usuario por otro para una API distinta, con el mismo usuario. |
| **`oid`** | Object id del usuario o identidad en el tenant. |
| **OIDC (OpenID Connect)** | Capa de identidad sobre OAuth 2.0. |
| **OpenTelemetry (OTel)** | Estándar abierto de trazas, métricas y logs. |
| **Probe** | Verificación periódica de salud que hace Container Apps. |
| **`ProductAnswer`** | Salida estructurada de Productos: `answer` y `sources`. |
| **Prompt injection** | Ataque que introduce instrucciones maliciosas en el texto que lee el modelo. |
| **Prompt Shields** | Clasificadores de Foundry para jailbreak e indirect attack. |
| **Proxy de Productos** | Agente local que representa a Productos en el handoff y lo llama por A2A. |
| **RAG** | Retrieval-Augmented Generation: recuperar contexto antes de generar. |
| **RAI policy** | Política de contenido (Responsible AI) de Foundry (`banking-agent-guardrails`). |
| **Rate limit** | Límite de solicitudes por usuario (`TokenBucket`). |
| **`reasoning_effort`** | Esfuerzo de razonamiento de un modelo de razonamiento; aquí `low`. |
| **Red teaming** | Pruebas adversariales que buscan romper la seguridad del agente. |
| **Refresh token** | Token para obtener nuevos access tokens sin volver a iniciar sesión. |
| **Reranking** | Reordenar resultados con un modelo semántico. |
| **Responses API** | API de OpenAI para respuestas con herramientas; la usa `FoundryChatClient`. |
| **Revisión (Container Apps)** | Versión inmutable de la configuración de una app. |
| **`roles`** | Claim con los app roles de un token de aplicación. |
| **RS256** | Firma RSA con SHA-256; el único algoritmo aceptado. |
| **Scale to zero** | Bajar a 0 réplicas sin tráfico. |
| **`scp`** | Claim con los scopes delegados. |
| **Scope** | Permiso delegado que una API expone (`access_as_user`, `tools.access`). |
| **Seed** | Datos sintéticos reproducibles (`mcp_server/seed.py`). |
| **Semantic ranker** | Reranker de AI Search; obligatorio para la knowledge base. |
| **`SendMessage`** | Método JSON-RPC de A2A para enviar un mensaje. |
| **Service principal** | Instancia de una app en un tenant, a la que se asignan permisos. |
| **Sesión durable** | Conversación persistida en SQL que cualquier réplica puede retomar. |
| **Span** | Tramo de una traza con duración y atributos. |
| **Streamable HTTP** | Transporte de MCP sobre un único endpoint HTTP. |
| **Subject (OIDC)** | Identificador del emisor federado; en GitHub, repositorio y rama en forma inmutable. |
| **Superstep** | Ronda de ejecución de un workflow de MAF; al final se guarda el checkpoint. |
| **System-assigned / user-assigned** | Managed identity atada a un recurso / independiente y reutilizable. |
| **Task (A2A)** | Unidad de trabajo con estado (`TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`). |
| **Tenant** | Instancia aislada de Entra ID (workforce o External ID). |
| **`tid`** | Claim con el id del tenant. |
| **Timeout** | Tiempo máximo de espera de una llamada. |
| **Token bucket** | Algoritmo de rate limit: fichas que se recargan a ritmo fijo. |
| **Tool calling** | Capacidad del modelo de pedir la ejecución de herramientas. |
| **`traceparent`** | Header W3C que propaga el contexto de traza entre servicios. |
| **Traza** | Recorrido completo de una petición con un trace id. |
| **TPM** | Tokens por minuto: capacidad de un despliegue. |
| **User flow** | Experiencia de inicio de sesión de External ID (`banco-andino-signin`). |
| **`validate_jwt`** | Función única de validación de tokens (`mcp_server/auth.py`). |
| **Walking skeleton** | Primera versión mínima desplegada de punta a punta (fase 1.5). |
| **Workflow (MAF)** | Grafo de ejecutores que intercambian mensajes por supersteps. |
| **Workforce (tenant)** | Tenant de empleados y servicios de la organización. |
| **`workflow_dispatch`** | Disparador manual de un workflow de GitHub Actions. |
