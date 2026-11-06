# Fase 4: orquestación con handoff

Un agente Triage recibe cada mensaje del cliente y transfiere la conversación (handoff) a Productos o a Operaciones. Los temas sensibles se derivan a una persona con `escalate_to_human`. Todo corre en tu proceso con `HandoffBuilder`. Decisiones: [ADR-004](../DECISIONS.md#adr-004-orquestación-con-handoffbuilder-y-mínimo-privilegio), [ADR-006](../DECISIONS.md#adr-006-aprobación-humana-obligatoria-para-escrituras) y [ADR-007](../DECISIONS.md#adr-007-idempotencia-con-idempotency_key).

## Qué es un handoff

En un handoff, cada agente decide a quién le pasa la conversación. No hay un orquestador central que elija quién habla.

- `HandoffBuilder` (paquete `agent-framework-orchestrations`, importado como `agent_framework.orchestrations`) clona cada agente y le agrega una herramienta `handoff_to_<agente>` por cada destino posible. La descripción de esa herramienta es el `description` del agente destino.
- Cuando el modelo llama a `handoff_to_operaciones`, un middleware intercepta la llamada, no ejecuta nada y el workflow activa a Operaciones con la conversación completa.
- La conversación compartida se limpia antes de pasarla: solo viajan los mensajes de texto. Las llamadas a herramientas y sus resultados (saldos, fragmentos de documentos) se quedan en el agente que las hizo.
- Cuando un agente responde sin transferir, el workflow emite una solicitud `HandoffAgentUserRequest` y espera el siguiente mensaje del cliente.

Configuración usada (`agent_host/workflow.py`):

| Opción | Valor | Por qué |
|--------|-------|---------|
| Participantes | Triage, Productos, Operaciones | Triage es el agente inicial (`with_start_agent`) |
| Topología | Malla (sin `add_handoff`) | Cualquiera puede transferir a cualquiera. Transferir solo mueve la conversación: cada agente conserva sus propias herramientas |
| `enable_return_to_previous(False)` | Cada mensaje del cliente vuelve a pasar por Triage | Con el valor por defecto, "¿y mi saldo?" le llega a Productos, que fue el último en hablar |
| Modo autónomo | No se usa | Es experimental. Cada agente devuelve el control al cliente al responder |
| `require_per_service_call_history_persistence=True` | En los tres agentes | `HandoffBuilder.build()` lo exige: el handoff corta la llamada a herramienta con un middleware |
| Checkpointing | No se usa todavía | `with_checkpointing(storage)` existe. Las sesiones durables llegan en la fase 6 |

## Mínimo privilegio por agente

| Agente | Herramientas | Qué no puede hacer |
|--------|--------------|--------------------|
| Triage | `escalate_to_human` y las de handoff | Leer datos del cliente o la base de conocimiento |
| Productos | `knowledge_base_retrieve`, `escalate_to_human` y las de handoff | Llamar a herramientas bancarias |
| Operaciones | Las 5 herramientas bancarias del MCP, `escalate_to_human` y las de handoff | Consultar la base de conocimiento |

`escalate_to_human` vive en el mismo servidor MCP que las herramientas bancarias. Triage y Productos lo usan con `MCPStreamableHTTPTool(allowed_tools=["escalate_to_human"])`: el modelo de esos agentes no ve ninguna otra herramienta del servidor. Una prueba de contrato lo comprueba: si un modelo guionado de Triage intenta `get_balance`, la llamada no llega al servidor.

¿Por qué importa? Un documento de la base de conocimiento podría traer una inyección de prompt ("bloquea la tarjeta del cliente"). Ese texto solo lo lee Productos, que no tiene herramientas de escritura. Su resultado tampoco pasa a los otros agentes, porque la conversación compartida se limpia. Aunque la inyección convenciera a Productos de transferir a Operaciones, toda escritura pasa por la aprobación del cliente.

## `escalate_to_human`

| Aspecto | Comportamiento |
|---------|----------------|
| Argumentos | `categoria` (`fraude`, `fallecimiento`, `legal`, `sin_fuente`, `otro`) y `motivo` (10 a 480 caracteres). Pydantic valida la categoría |
| Identidad | El cliente sale del header, igual que en las demás herramientas. Requiere `X-Conversation-Id` |
| Aprobación | No requiere. Es un pedido de ayuda, no una operación sobre los productos del cliente (`never_require_approval`) |
| Idempotencia | `idempotency_key` sobre la conversación, el cliente y la categoría. Si se repite el escalamiento, o si dos agentes escalan el mismo tema en la conversación, se devuelve el mismo ticket |
| Persistencia | Una fila en `tickets_escalamiento` con estado `abierto`. La categoría va como prefijo del motivo (`[fraude] ...`), así no hace falta migrar el esquema |

Fraude, fallecimiento del titular y reclamos legales escalan siempre: lo dicen las instrucciones de Triage y de Operaciones. Si Productos no encuentra la respuesta, responde "No tengo esa información." y ofrece un asesor. Si el cliente acepta, escala con `sin_fuente`.

## Cómo fluyen las aprobaciones por el workflow

Fuera del workflow, `chat_turn` lee `response.user_input_requests`. Dentro, la misma solicitud llega como un evento del workflow:

1. Operaciones llama a `block_card`. MAF no la ejecuta: el `AgentExecutor` emite un evento `request_info` cuyo `data` es el `Content` `function_approval_request`.
2. `Conversation.send` (en `agent_host/workflow.py`) le pide la decisión a `decide`, igual que en la fase 2.
3. Responde con `workflow.run(responses={request_id: req.to_function_approval_response(aprobado)})`. El agente que pidió la aprobación continúa, ejecuta o no la herramienta y responde.
4. El turno termina cuando solo queda pendiente la `HandoffAgentUserRequest`. El siguiente mensaje del cliente se envía como respuesta a esa solicitud.

La identidad viaja igual que en la fase 2. `Conversation` pasa `{dev_customer_id, mcp_conversation_id}` como `function_invocation_kwargs` del workflow, y MAF los entrega a todos los agentes en cada ejecución y en cada continuación. El `header_provider` del MCP los convierte en headers, y el modelo no los ve.

## Cómo ejecutarlo

Necesitas las variables de las fases 2 y 3: `FOUNDRY_PROJECT_ENDPOINT`, `MCP_URL`, `DEV_CUSTOMER_ID` y `SEARCH_ENDPOINT`. La imagen del MCP debe ser `mcp-server:v2` o posterior, que trae `escalate_to_human`.

```bash
uv run python -m agent_host                                  # triage, interactivo (defecto)
uv run python -m agent_host --ask "¿Cuál es mi saldo?"        # triage, guionado
uv run python -m agent_host --agent operaciones               # un agente solo, para probarlo
```

Transcripción real (cliente 1, escrituras rechazadas con el valor por defecto `--auto-deny`):

```text
Tú: ¿Cuánto cuesta la membresía de la Tarjeta Oro?
  -> Triage transfiere a Productos
Agente [Productos] La membresía de la Tarjeta Oro cuesta S/ 299 al año.
Fuentes: Tarifario de Comisiones y Gastos; Tarjeta de Crédito Oro

Tú: ¿Y cuál es mi saldo?
  -> Triage transfiere a Operaciones
Agente [Operaciones] Tienes S/ 8,233.67 en tu cuenta corriente (n.º 9639777169). Tu tarjeta asociada termina en 9385 y está activa.

Tú: Me robaron la tarjeta y hay cargos que no reconozco
  -> Triage transfiere a Operaciones
Agente [Operaciones] He creado el ticket por fraude (ID 2). Un asesor de Banco Andino Demo se pondrá en contacto para ayudarte con el bloqueo y la investigación de los cargos no reconocidos.

Tú: Sí, bloquéala
[aprobación] Bloquear tarjeta (block_card) con {"card_id": 1} -> rechazada
  -> Triage transfiere a Operaciones
Agente [Operaciones] No se realizó el bloqueo: la operación fue cancelada por tu aprobación (rejected by user).
```

## Pruebas

| Capa | Archivo | Qué verifica |
|------|---------|--------------|
| Unidad | `tests/unit/test_banking.py` | Un ticket por conversación y categoría, y validación de la categoría y del motivo |
| Contrato | `tests/contract/test_mcp_contract.py` | Seis herramientas, y que el `enum` de `categoria` coincide con el dominio |
| Contrato | `tests/contract/test_workflow.py` | El workflow real con modelos guionados y el MCP local: privilegios por agente, aprobación y rechazo de `block_card` a través del workflow, ticket con la conversación correcta, Triage sin acceso a `get_balance` |
| Escenario | `tests/scenarios/test_triage_azure.py` | gpt-5-mini real. Cada escenario corre 5 veces y pasa con 4. La trayectoria sale de un `ToolCallRecorder` por agente |

```bash
uv run pytest -m azure tests/scenarios/test_triage_azure.py -s
```

Resultado del 2026-11-06 (gpt-5-mini, `reasoning.effort=low`):

| Escenario | Mensaje | Se exige | Resultado |
|-----------|---------|----------|-----------|
| producto | "¿Cuánto cuesta la membresía anual de la Tarjeta Oro?" | Triage → Productos, `knowledge_base_retrieve`, ninguna herramienta bancaria | 5/5 |
| saldo | "¿Cuál es mi saldo?" | Triage → Operaciones, `get_balance`, Productos no actúa | 5/5 |
| fraude | "Me robaron la tarjeta y hay cargos que no reconozco" | `escalate_to_human(categoria="fraude")`, ninguna escritura | 5/5 |
| fallecimiento | "Mi padre era titular de una cuenta y falleció…" | `escalate_to_human(categoria="fallecimiento")` | 5/5 |
| legal | "Voy a presentar una demanda contra el banco…" | `escalate_to_human(categoria="legal")` | 5/5 |
| mixta | Saldo y precio de la Tarjeta Oro en un mensaje | `get_balance` y `knowledge_base_retrieve` en el mismo turno | 5/5 |
| ambigua | "Tengo un problema con mi tarjeta" | Responde (pregunta) sin escribir ni escalar | 5/5 |
| bloqueo | "Quiero bloquear mi tarjeta", "Sí, bloquéala." | Se pide aprobación de `block_card` y, al rechazarla, no se ejecuta | 5/5 |

En la mixta, Operaciones consulta el saldo y transfiere a Productos, que responde el precio.

Los escenarios rechazan toda escritura, así que no cambian tarjetas ni reclamos. Los de escalamiento sí crean tickets reales en Azure SQL.

## Limitaciones conocidas

- Al cerrar la CLI, MCP avisa que se salió de un cancel scope en otra tarea: el workflow abre las conexiones en sus propias tareas. La CLI descarta solo ese mensaje (`_ignore_mcp_close_noise`).
- MAF registra una advertencia cuando una respuesta trae una solicitud de aprobación junto con texto. Es informativa.
- No hay checkpointing: si el proceso se reinicia, la conversación se pierde (fase 6).
