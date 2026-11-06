# Fase 1.5: el servidor MCP en Azure

En esta fase el mismo servidor MCP de la [fase 1](fase-1-mcp.md) corre en Azure Container Apps contra Azure SQL Database, sin contraseñas: el contenedor se autentica con su identidad administrada y tú, desde tu máquina, con tu sesión de `az login`. La infraestructura está en [`infra/`](../../infra/README.md).

## Qué cambió en el código

| Pieza | Cambio |
|-------|--------|
| Driver | `mssql-python`, el driver oficial de Microsoft. SQLAlchemy 2.1 trae su dialecto (`mssql+mssqlpython`). Incluye el driver ODBC dentro del paquete: no hay que instalar ODBC en tu máquina ni en CI. |
| `mcp_server/db.py` | Las columnas de texto usan `Unicode`/`UnicodeText` (`NVARCHAR` en SQL Server) para que los nombres con tildes se guarden bien. En SQLite no cambia nada. `pool_pre_ping` en Azure SQL, porque la base serverless se pausa y corta las conexiones inactivas. |
| `mcp_server/server.py` | `GET /healthz` responde `ok` para las sondas de Container Apps. No consulta la base: si lo hiciera, la base nunca se pausaría. |
| `Dockerfile` | `python:3.12-slim`, `uv sync --frozen --no-dev`, usuario sin privilegios, escucha en `0.0.0.0:8000` (`MCP_HOST`, `MCP_PORT`). |

SQLite sigue siendo el valor por defecto: sin `DATABASE_URL` todo funciona como en la fase 1.

### URLs de conexión

| Dónde | `DATABASE_URL` |
|-------|----------------|
| Contenedor (lo define Terraform) | `mssql+mssqlpython://@<servidor>.database.windows.net:1433/sqldb-banking?Authentication=ActiveDirectoryMSI&UID=<client id de id-mcp-server>&Encrypt=yes` |
| Tu máquina (seed) | `mssql+mssqlpython://@<servidor>.database.windows.net:1433/sqldb-banking?Authentication=ActiveDirectoryDefault&Encrypt=yes` |

`ActiveDirectoryDefault` usa `DefaultAzureCredential`, que toma tu sesión de Azure CLI. `ActiveDirectoryMSI` con `UID` usa la identidad administrada asignada por el usuario.

## Pasos después del `terraform apply`

Todos los comandos se ejecutan desde la raíz del repo, en Git Bash, con `az login` en la suscripción correcta.

```bash
SQL=$(terraform -chdir=infra output -raw sql_server_fqdn)
ACR=$(terraform -chdir=infra output -raw acr_name)
```

### 1. Crear el usuario de la identidad en la base

Conéctate a `sqldb-banking` (no a `master`) como administrador Entra y ejecuta [`infra/sql/create-mcp-user.sql`](../../infra/sql/create-mcp-user.sql):

```bash
sqlcmd -S "$SQL" -d sqldb-banking --authentication-method ActiveDirectoryDefault -i infra/sql/create-mcp-user.sql
```

Si no tienes `sqlcmd` (`sqlcmd --version`), abre la base en el portal de Azure, entra en **Editor de consultas**, inicia sesión con Entra y pega el contenido del archivo.

### 2. Cargar los datos sintéticos

El seed crea las tablas y los datos con tu identidad (eres el admin; la identidad del contenedor solo lee y escribe datos):

```bash
export DATABASE_URL="mssql+mssqlpython://@$SQL:1433/sqldb-banking?Authentication=ActiveDirectoryDefault&Encrypt=yes"
uv run python -m mcp_server.seed
unset DATABASE_URL
```

Puedes repetirlo cuando quieras: borra y recrea todas las tablas, así que siempre deja los mismos datos. También borra la auditoría y las claves de idempotencia.

La base se pausa tras un rato sin uso. Si la primera conexión falla con el error 40613 ("database is not currently available"), espera un minuto y vuelve a intentar.

### 3. Publicar la imagen

ACR construye la imagen en Azure; no necesitas Docker local:

```bash
az acr build --registry "$ACR" --image mcp-server:v1 .
```

### 4. Apuntar el contenedor a la imagen

En `infra/dev.tfvars` define la variable `mcp_image`:

```hcl
mcp_image = "<acr_login_server>/mcp-server:v1"
```

`<acr_login_server>` es `terraform -chdir=infra output -raw acr_login_server`. Luego:

```bash
terraform -chdir=infra plan -var-file=dev.tfvars -out=tfplan
terraform -chdir=infra apply tfplan
```

Con una imagen distinta del placeholder, Terraform cambia el puerto del ingress de 80 a 8000. Las demás variables del contenedor (`AUTH_MODE=dev`, `MCP_HOST`, `DATABASE_URL`, `AZURE_CLIENT_ID`) ya las define Terraform.

### 5. Probar contra la URL pública

El ingress solo acepta tu IP (`client_ip`).

```bash
MCP_URL=$(terraform -chdir=infra output -raw mcp_url)
curl -s "${MCP_URL%/mcp}/healthz"   # ok
```

La primera petición puede tardar: la app escala desde 0 réplicas y la base puede estar pausada.

Con MCP Inspector (`npx @modelcontextprotocol/inspector`):

1. **Transport Type: Streamable HTTP** y la URL de `$MCP_URL`.
2. Headers: `X-Dev-Customer-Id: 1` y `X-Conversation-Id: azure-1`.
3. Lista las herramientas y ejecuta `get_balance`. Repite las pruebas de la [fase 1](fase-1-mcp.md#probar-con-mcp-inspector).

Para ver los logs del contenedor:

```bash
az containerapp logs show -g "$(terraform -chdir=infra output -raw resource_group)" -n mcp-server --follow
```

## Problemas frecuentes

| Síntoma | Causa probable |
|---------|----------------|
| `Login failed for user '<token-identified principal>'` en el contenedor | Falta el paso 1 (usuario `id-mcp-server` en la base). |
| `CREATE TABLE permission denied` al arrancar el contenedor | Falta el paso 2 (seed). Al arrancar, el servidor crea las tablas que no existan, y la identidad no tiene permisos de DDL. |
| 403 desde Container Apps | Tu IP cambió: actualiza `client_ip` y vuelve a aplicar. |
| El seed no conecta desde tu máquina | `az login` en otra cuenta o suscripción, o tu IP no es la de `client_ip` (firewall de SQL). |
