# Fase 3: base de conocimiento con Foundry IQ

El agente Productos responde preguntas de productos, tarifas y procesos solo con lo que recupera de una base de conocimiento de Foundry IQ, y cita el documento de donde sale cada dato. Corre en tu proceso, igual que Operaciones; en la fase 7 pasa a ser remoto vía A2A. Decisiones: [ADR-003](../DECISIONS.md#adr-003-separación-estricta-de-fuentes-de-verdad), [ADR-012](../DECISIONS.md#adr-012-foundry-iq-sobre-ai-search-free) y [ADR-018](../DECISIONS.md#adr-018-respuestas-consistentes-y-fundamentadas).

## Qué hace Foundry IQ por dentro

Foundry IQ es la capa de conocimiento de Foundry; por debajo es Azure AI Search. Tú creas dos objetos y el servicio genera el resto.

| Paso | Qué pasa | Dónde se ve |
|------|----------|-------------|
| Fuente | Los 8 PDF viven en el contenedor `productos` del storage | `knowledge/publish.py` |
| Knowledge source (`ks-productos`) | Al crearla, AI Search genera un data source, un skillset, un índice y un indexer con nombres fijos (`ks-productos-*`) | `knowledge/ingest.py` imprime `createdResources` |
| Ingesta | El indexer lee cada PDF con la identidad del servicio y extrae el texto (`contentExtractionMode = minimal`) | Estado en `knowledgesources/ks-productos/status` |
| Chunking | El skillset (`SplitSkill`) parte el texto en fragmentos de hasta 2 000 caracteres con 200 de solapamiento. Los 8 documentos quedan en 11 fragmentos: los 3 más largos se parten en dos | Índice `ks-productos-index` |
| Embeddings | Cada fragmento pasa por `text-embedding-3-small` (vector de 1 536 dimensiones en `snippet_vector`) en tu cuenta Foundry, con la identidad del servicio de búsqueda (sin claves) | Skillset `ks-productos-skillset` |
| Búsqueda híbrida | Cada consulta corre en paralelo por texto (BM25) y por vector (la consulta se vectoriza con el mismo modelo); los resultados se fusionan | Actividad `azureBlob` de la respuesta |
| Reranking | El semantic ranker reordena los fragmentos y les pone un `rerankerScore` | `references[].rerankerScore` |
| Recuperación | La knowledge base (`kb-productos`) devuelve los fragmentos y una referencia por fragmento con la URL del PDF. Es **extractiva**: no redacta la respuesta | `knowledge_base_retrieve` |
| Síntesis | La hace nuestro agente con gpt-5-mini, no Foundry IQ (la síntesis de Foundry IQ está en preview) | `agent_host/productos.py` |

La knowledge base no tiene un modelo propio y usa esfuerzo de razonamiento `minimal`: no planifica consultas, solo recupera.

## Archivos

| Archivo | Qué hace |
|---------|----------|
| `knowledge/docs/*.md` | 8 documentos ficticios: tarjetas Clásica y Oro, cuenta de ahorros, cuenta corriente, tarifario, reclamos, bloqueo y seguridad, preguntas frecuentes. El H1 es el título que se cita. |
| `knowledge/publish.py` | Renderiza cada markdown a PDF con `fpdf2` (repite el título arriba de cada página) y lo sube a Blob con un token Entra. |
| `knowledge/ingest.py` | Crea la knowledge source y la knowledge base, espera la ingesta e imprime el estado. Se puede volver a correr. |
| `agent_host/productos.py` | Instrucciones, salida estructurada (`ProductAnswer`), herramienta MCP de la knowledge base y token de AI Search. |
| `agent_host/config.py` | `load_productos_settings`: `FOUNDRY_PROJECT_ENDPOINT`, `SEARCH_ENDPOINT` y `KNOWLEDGE_BASE` (por defecto `kb-productos`). No necesita `MCP_URL` ni `DEV_CUSTOMER_ID`. |
| `agent_host/cli.py` | `--agent productos` (por defecto sigue siendo `operaciones`). |
| `evals/retrieval_cases.jsonl`, `evals/run_retrieval.py` | Evaluación de recuperación: pregunta y documento esperado. |
| `tests/unit/test_productos.py` | Config, URL MCP, caché del token, herramienta de solo lectura. Sin red. |
| `tests/scenarios/test_productos_azure.py` | Escenarios con gpt-5-mini y la knowledge base real (`-m azure`). |

### El agente

- **Herramienta.** `MCPStreamableHTTPTool` contra `{search}/knowledgebases/kb-productos/mcp?api-version=2026-08-01-preview`, limitada a `knowledge_base_retrieve` y sin aprobación (solo lee).
- **Autenticación.** `SearchTokenHeaders` es el `header_provider`: pide un token para `https://search.azure.com/.default` y lo reutiliza hasta 5 minutos antes de que venza. Tu usuario necesita `Search Index Data Reader` (Terraform te da `Search Index Data Contributor`, que lo incluye); en Azure lo usará `id-agent-host`.
- **Salida estructurada.** Cada respuesta es un `ProductAnswer` con `answer` y `sources` (títulos de los documentos usados). La CLI imprime las fuentes debajo de la respuesta.
- **Reglas.** Solo responde con lo recuperado; si no hay fuente, dice "No tengo esa información." y sugiere hablar con un asesor; nunca da datos personales del cliente (eso es de Operaciones, ADR-003).

## Cómo ejecutarlo

Requisitos: `az login` y los recursos de la fase 3 aplicados (ver [infra/README.md](../../infra/README.md)).

```bash
export STORAGE_ACCOUNT_NAME=$(terraform -chdir=infra output -raw storage_account_name)
export STORAGE_ACCOUNT_ID=$(terraform -chdir=infra output -raw storage_account_id)
export SEARCH_ENDPOINT=$(terraform -chdir=infra output -raw search_endpoint)
export FOUNDRY_OPENAI_ENDPOINT=$(terraform -chdir=infra output -raw foundry_openai_endpoint)
export FOUNDRY_PROJECT_ENDPOINT=$(terraform -chdir=infra output -raw foundry_project_endpoint)

uv run --group knowledge python knowledge/publish.py   # md -> PDF -> Blob
uv run python knowledge/ingest.py                      # knowledge source + knowledge base
uv run python knowledge/ingest.py --reindex            # después de cambiar documentos

uv run python -m agent_host --agent productos          # chat
uv run python evals/run_retrieval.py                   # evaluación de recuperación
uv run pytest -m azure tests/scenarios/test_productos_azure.py -v -s
```

> En Git Bash para Windows, exporta `MSYS_NO_PATHCONV=1` antes de `ingest.py`: si no, Git Bash convierte `/subscriptions/...` de `STORAGE_ACCOUNT_ID` en una ruta de Windows y la knowledge source falla con "Invalid resource ID".

Salida de la ingesta (primera vez):

```text
knowledge source ks-productos creada
  datasource: ks-productos-datasource
  indexer: ks-productos-indexer
  skillset: ks-productos-skillset
  index: ks-productos-index
knowledge base kb-productos creada o actualizada
  en cola...
sincronización terminada 2026-11-06T19:30:59.836Z | procesados 8 | fallidos 0
MCP: https://srch-banking-agent-ke9ih.search.windows.net/knowledgebases/kb-productos/mcp?api-version=2026-08-01-preview
```

## Transcripción real

Ejecutada el 2026-11-06 con gpt-5-mini (`reasoning.effort = low`):

```text
$ uv run python -m agent_host --agent productos --ask "¿Cuánto cuesta la membresía de la Tarjeta Oro y cómo la exonero?" --ask "¿Cuál es la tasa de un crédito hipotecario?" --ask "¿Cuál es mi saldo?"
Agente productos | base de conocimiento kb-productos

Tú: ¿Cuánto cuesta la membresía de la Tarjeta Oro y cómo la exonero?
Agente: La membresía anual de la Tarjeta Oro es S/ 299. Se exonera si tus consumos suman S/ 24,000 o más en los 12 meses previos al cobro.
Fuentes: Tarjeta de Crédito Oro; Tarifario de Comisiones y Gastos

Tú: ¿Cuál es la tasa de un crédito hipotecario?
Agente: No tengo esa información. Te sugiero hablar con un asesor de Banco Andino Demo para consultar las tasas de créditos hipotecarios.

Tú: ¿Cuál es mi saldo?
Agente: No puedo consultar saldos aquí. Para ver tu saldo, ingresa a la app o web del banco o contacta al asistente de operaciones de Banco Andino Demo.
```

## Evaluaciones

Las dos evaluaciones se reportan por separado (ADR-018):

| Evaluación | Qué mide | Resultado (2026-11-06) |
|------------|----------|------------------------|
| Recuperación (`evals/run_retrieval.py`, 10 casos) | Si la knowledge base devuelve el documento esperado. Llama a `retrieve` directo, sin agente | hit@1 10/10, hit@k 10/10 |
| Respuesta (`tests/scenarios/test_productos_azure.py`, 4 escenarios) | Cifra correcta con cita válida, la misma pregunta 5 veces con la misma cifra y fuente, "No tengo esa información." sin fuente, rechazo de datos personales | 4/4 |

El 100 % de recuperación refleja un corpus chico y temático (8 documentos con títulos distintos): no es una cifra que se sostenga con un corpus real. Agrega casos ambiguos o entre documentos a `retrieval_cases.jsonl` cuando crezca la base.

## Hallazgos de la implementación

- **Semantic ranker obligatorio.** La knowledge base responde "requires Semantic Search to be enabled" si el semantic ranker está apagado. En Free se activa el plan free con `azapi` (ver [infra/README.md](../../infra/README.md#notas-de-la-fase-3)).
- **El endpoint MCP es preview.** Sin modelo en la knowledge base, solo acepta esfuerzo de razonamiento `minimal`, y ese valor solo se guarda con la API `2026-08-01-preview`. `ingest.py` crea la knowledge source con la API GA (`2026-04-01`) y la knowledge base con la preview.
- **`knowledgeRetrieval`.** El servicio queda en el plan `free`: `standard` exige Basic o superior. Aun con `minimal`, cada consulta reporta ~4 000 tokens de razonamiento agéntico que se descuentan de la cuota mensual gratuita.
- **Citas.** Cada fragmento trae el título del documento en su primera línea cuando es el inicio del PDF; los fragmentos de continuación pueden no traerlo, y entonces el agente cita el nombre del PDF de la referencia. Las pruebas aceptan ambos.
