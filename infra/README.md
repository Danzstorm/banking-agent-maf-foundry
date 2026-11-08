# Infraestructura (Fase 1.5: walking skeleton)

Terraform que crea la base mínima en Azure para que el servidor MCP corra en Container Apps contra Azure SQL, con Foundry y gpt-5-mini listos para la fase 2. Todo queda en el resource group `rg-banking-agent-dev`, con los tags `project=banking-agent` y `env=dev`.

## Región: South Central US

Todo se despliega en `southcentralus`, una sola región. Verificado el 2026-11-06 sobre esta suscripción:

| Requisito | eastus2 | eastus | centralus | southcentralus |
|-----------|---------|--------|-----------|----------------|
| gpt-5-mini GlobalStandard (`az cognitiveservices model list`) | Sí | Sí | Sí | Sí |
| Cuota gpt-5-mini GlobalStandard (`az cognitiveservices usage list`) | 0/1000 | 0/1000 | 0/1000 | 0/1000 |
| Azure SQL (API `locations/{r}/capabilities`) | Restringida | Restringida | Disponible | Disponible |
| AI Search: crear servicios nuevos | No (alta demanda) | Sí | Sí | Sí |
| AI Search Free con agentic retrieval (fase 3) | Sí, pero no se puede crear | Sí | No | Sí |
| Container Apps y Foundry projects | Sí | Sí | Sí | Sí |

South Central US es la única de las cuatro que cumple todo. Fuente de AI Search: [Supported regions](https://learn.microsoft.com/azure/search/search-region-support).

> El free offer de Azure SQL fija su región con la primera base gratuita de la suscripción. Si ya tienes una base free en otra región, la creación puede fallar: usa esa región en `location` o elimina la otra base.

## Recursos

| Recurso | Para qué | Costo en reposo |
|---------|----------|-----------------|
| Resource group | Contenedor de todo | 0 |
| Log Analytics + Application Insights | Logs de Container Apps y trazas OTel (cuota diaria de 1 GB) | ~0 (pago por GB ingerido) |
| Foundry account (AIServices) + project | Plataforma del modelo, tracing y evaluaciones. Solo Entra ID (`local_auth_enabled = false`) | 0 |
| Deployment `gpt-5-mini` (GlobalStandard, 50K TPM) | Modelo del agente | 0 (pago por token) |
| RAI policy `banking-agent-guardrails` | DefaultV2 + jailbreak e indirect attack en modo bloqueo (ADR-017) | 0 |
| Azure SQL server + `sqldb-banking` | Core bancario. Solo Entra; free offer con AutoPause (ADR-011) | 0 |
| Identidad `id-mcp-server` | El contenedor MCP la usa para ACR y SQL | 0 |
| Identidad `id-agent-host` | El agente host (fase 2) la usa para Foundry | 0 |
| Container Registry Basic | Imágenes propias, sin usuario admin | ~USD 5/mes (~0,17/día) |
| Container Apps environment + `mcp-server` | Servidor MCP; 0 a 1 réplicas | 0 con 0 réplicas |
| Storage account `stbankingagent<sufijo>` + contenedor `productos` (fase 3) | PDF de productos para Foundry IQ. Solo Entra (`shared_access_key_enabled = false`) | Centavos al mes |
| AI Search Free `srch-banking-agent-<sufijo>` (fase 3) | Knowledge source y knowledge base de Foundry IQ. Solo Entra (`local_authentication_enabled = false`), identidad del sistema, semantic ranker en plan free | 0 |
| Deployment `text-embedding-3-small` (GlobalStandard, 50K TPM, fase 3) | Embeddings de los fragmentos y de las consultas | 0 (pago por token) |
| Key Vault `kv-banking-<sufijo>` + certificado `agent-obo` (fase 5) | Credencial del OBO del agente; solo RBAC | Centavos al mes (operaciones) |
| App registrations en el tenant External ID (fase 5) | `banking-cli`, `banking-agent-api`, `banking-mcp-api` | 0 |
| Presupuesto USD 20/mes | Alertas al 50 % y 100 % real, y 100 % pronosticado | 0 |

**Costo en reposo estimado: ~USD 0,17 al día**, casi todo de ACR. El resto cobra solo por uso (tokens, segundos de contenedor, GB de logs). Aun así, ejecuta `terraform destroy` al terminar cada sesión (ADR-021).

Roles asignados:

- `Foundry User` (antes "Azure AI User") sobre la cuenta Foundry para `id-agent-host` y para ti (desarrollo local). Cubre Responses API, tracing y evaluaciones. Si solo necesitas inferencia, `Foundry Project Runtime User` es más restrictivo.
- `AcrPull` sobre el ACR para `id-mcp-server`.
- Fase 3: la identidad de AI Search tiene `Storage Blob Data Reader` sobre el storage (el indexer lee los PDF) y `Cognitive Services User` sobre Foundry (embeddings). Tú tienes `Storage Blob Data Contributor`, `Search Service Contributor` y `Search Index Data Contributor` (subir PDF, crear la knowledge base y consultarla). `id-agent-host` tiene `Search Index Data Reader`.
- Tú quedas como administrador Entra del servidor SQL.

## Identidad (fase 5)

`identity.tf` crea, con el alias `azuread.external`, las apps `banking-cli`, `banking-agent-api` y `banking-mcp-api` en el tenant de Entra External ID (`external_tenant_id`), sus service principals, la preautorización y los consentimientos de administrador. También crea el Key Vault `kv-banking-<sufijo>` (RBAC, sin purge protection, 7 días de soft delete) con el certificado autofirmado `agent-obo`, cuya parte pública se sube a `banking-agent-api`. Roles: tú tienes `Key Vault Certificates Officer` y `Key Vault Secrets User`; `id-agent-host` tiene `Key Vault Secrets User`. El user flow y los clientes de prueba se crean con los scripts de `infra/identity/` (Graph). Detalle: [docs/guides/fase-5-identidad.md](../docs/guides/fase-5-identidad.md).

El servidor MCP corre con `AUTH_MODE=entra` y valida el JWT en su código. El ingress sigue restringido a tu IP (`client_ip`) como defensa en profundidad mientras el agente host corre en tu máquina. Si tu IP cambia, actualiza `client_ip` y vuelve a aplicar.

## Uso

Requisitos: Terraform >= 1.9 y Azure CLI con sesión iniciada (`az login`) en la suscripción correcta.

```bash
cd infra
cp terraform.tfvars.example dev.tfvars   # dev.tfvars está en .gitignore
# Completa subscription_id, alert_email y client_ip (curl -s https://api.ipify.org)

terraform init
terraform plan -var-file=dev.tfvars -out=tfplan
terraform apply tfplan

# Al terminar la sesión
terraform destroy -var-file=dev.tfvars
```

El primer `apply` usa la imagen pública `mcr.microsoft.com/k8se/quickstart:latest` (escucha en el puerto 80) para que funcione antes de que exista nuestra imagen. Con la imagen propia el puerto pasa a 8000 automáticamente.

## Pasos después del apply

### 1. Crear el usuario de base de datos de la identidad

Conéctate a `sqldb-banking` (no a `master`) con tu cuenta Entra y ejecuta [`sql/create-mcp-user.sql`](sql/create-mcp-user.sql). Puedes usar el editor de consultas del portal o `sqlcmd`:

```bash
sqlcmd -S "$(terraform output -raw sql_server_fqdn)" -d sqldb-banking --authentication-method ActiveDirectoryDefault -i sql/create-mcp-user.sql
```

### 2. Cargar los datos sintéticos

Corre el seed desde tu máquina contra Azure SQL con tu identidad (eres el admin, así que también crea el esquema). La forma exacta de la URL depende de cómo el código Python termine de soportar `mssql-python`; el patrón esperado es:

```bash
export DATABASE_URL="mssql+mssqlpython://@$(terraform output -raw sql_server_fqdn):1433/sqldb-banking?Authentication=ActiveDirectoryDefault&Encrypt=yes"
uv run python -m mcp_server.seed
```

La base se pausa tras 15 minutos sin uso; la primera conexión tarda unos segundos en despertarla.

### 3. Publicar la imagen y actualizar el contenedor

Cuando exista un `Dockerfile` en la raíz del repo:

```bash
ACR=$(terraform -chdir=infra output -raw acr_name)
az acr build --registry "$ACR" --image mcp-server:v1 .
```

Luego, en `dev.tfvars`:

```hcl
mcp_image = "<acr_login_server>/mcp-server:v1"
```

y vuelve a ejecutar `plan` y `apply`. El contenedor descarga la imagen con `id-mcp-server`, sin contraseñas.

## Variables de entorno del contenedor

| Variable | Valor |
|----------|-------|
| `AUTH_MODE` | `entra` (fase 5; antes `dev`) |
| `ENTRA_TENANT_ID` | Tenant de External ID |
| `ENTRA_ISSUER` | `https://<tenant-id>.ciamlogin.com/<tenant-id>/v2.0` |
| `ENTRA_AUDIENCE` | Client id de `banking-mcp-api` (`aud` de los tokens v2) |
| `ENTRA_JWKS_URI` | `https://<subdominio>.ciamlogin.com/<tenant-id>/discovery/v2.0/keys` |
| `MCP_HOST` | `0.0.0.0` (el servidor escucha en `127.0.0.1` por defecto) |
| `DATABASE_URL` | URL de SQLAlchemy con `ActiveDirectoryMSI` y el client id de `id-mcp-server` |
| `AZURE_CLIENT_ID` | Client id de `id-mcp-server` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Desde un secret de Container Apps |

## Pendiente para fases siguientes

- Conectar Application Insights al proyecto Foundry (tracing): se hace desde el portal de Foundry o con una conexión `azapi` en la fase de observabilidad.

## Notas de la fase 7

`a2a.tf` crea la identidad `id-productos-a2a` (`AcrPull`, `Foundry User` y `Search Index Data Reader`), la app `banking-productos-a2a` en el tenant **workforce** (provider `azuread` por defecto) con el app role `Productos.Invoke` asignado solo a `id-agent-host`, y la Container App `productos-a2a` con ingress interno. Requiere permisos para crear app registrations y asignar app roles en ese tenant. Detalle: [docs/guides/fase-7-a2a.md](../docs/guides/fase-7-a2a.md).

## Notas de la fase 3

- **Managed identity en Free.** La documentación de AI Search pide Basic o superior para managed identity, pero la API la acepta en Free y la ingesta funciona con ella (verificado el 2026-11-06). Sin ella no habría cómo llegar a Foundry, que tiene la autenticación por clave deshabilitada.
- **Semantic ranker.** La knowledge base falla con "requires Semantic Search" si el semantic ranker está apagado. `azurerm` rechaza `semantic_search_sku` con `sku = "free"`, aunque la API lo acepta; por eso lo activa `azapi_update_resource.search_semantic`.
- **`knowledgeRetrieval`.** Queda en `free` (el valor por defecto): `standard` exige Basic o superior. El plan free da una cuota mensual de tokens; al agotarse, las consultas devuelven un error de pago requerido.
- **`storage_use_azuread = true`** en el provider: sin él, `azurerm` intenta leer el storage con clave compartida y falla con 403.
- La knowledge source y la knowledge base son objetos de data plane: los crea `knowledge/ingest.py`, no Terraform (ADR-012). `terraform destroy` las elimina junto con el servicio.
