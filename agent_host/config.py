"""Settings from environment variables. No secrets: Foundry auth comes from Entra (ADR-008)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigError(Exception):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class EntraSettings:
    """AUTH_MODE=entra: customer signs in to External ID; the host does OBO to the MCP API."""

    authority: str  # https://<subdomain>.ciamlogin.com/<tenant-id>
    cli_client_id: str
    agent_api_client_id: str
    mcp_api_client_id: str
    key_vault_uri: str
    cert_name: str

    @property
    def agent_scope(self) -> str:
        return f"api://{self.agent_api_client_id}/access_as_user"

    @property
    def mcp_scope(self) -> str:
        return f"api://{self.mcp_api_client_id}/tools.access"


@dataclass(frozen=True)
class Settings:
    project_endpoint: str
    model_deployment: str
    mcp_url: str
    dev_customer_id: int | None  # AUTH_MODE=dev only
    entra: EntraSettings | None = None  # AUTH_MODE=entra only


def _https_url(name: str, value: str, *, allow_http_localhost: bool = False) -> str:
    value = value.strip()
    url = urlparse(value)
    local = url.hostname in {"127.0.0.1", "localhost"}
    if url.scheme == "https" or (allow_http_localhost and url.scheme == "http" and local):
        return value
    raise ConfigError(f"{name} debe ser una URL https.")


ENTRA_VARS = (
    "ENTRA_AUTHORITY",
    "ENTRA_CLI_CLIENT_ID",
    "ENTRA_AGENT_API_CLIENT_ID",
    "ENTRA_MCP_API_CLIENT_ID",
    "KEY_VAULT_URI",
)


def _require(env: Mapping[str, str], names: tuple[str, ...]) -> None:
    missing = [name for name in names if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(f"Faltan variables de entorno: {', '.join(missing)}.")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """AUTH_MODE=dev (default) needs DEV_CUSTOMER_ID; AUTH_MODE=entra needs ENTRA_* vars."""
    env = os.environ if env is None else env
    _require(env, ("FOUNDRY_PROJECT_ENDPOINT", "MCP_URL"))
    mode = env.get("AUTH_MODE", "").strip() or "dev"
    customer, entra = None, None
    if mode == "dev":
        _require(env, ("DEV_CUSTOMER_ID",))
        raw_customer = env["DEV_CUSTOMER_ID"].strip()
        if not (raw_customer.isascii() and raw_customer.isdigit() and int(raw_customer) > 0):
            raise ConfigError("DEV_CUSTOMER_ID debe ser un entero positivo.")
        customer = int(raw_customer)
    elif mode == "entra":
        _require(env, ENTRA_VARS)
        entra = EntraSettings(
            authority=_https_url("ENTRA_AUTHORITY", env["ENTRA_AUTHORITY"]).rstrip("/"),
            cli_client_id=env["ENTRA_CLI_CLIENT_ID"].strip(),
            agent_api_client_id=env["ENTRA_AGENT_API_CLIENT_ID"].strip(),
            mcp_api_client_id=env["ENTRA_MCP_API_CLIENT_ID"].strip(),
            key_vault_uri=_https_url("KEY_VAULT_URI", env["KEY_VAULT_URI"]),
            cert_name=env.get("OBO_CERT_NAME", "").strip() or "agent-obo",
        )
    else:
        raise ConfigError("AUTH_MODE debe ser dev o entra.")
    return Settings(
        project_endpoint=_https_url("FOUNDRY_PROJECT_ENDPOINT", env["FOUNDRY_PROJECT_ENDPOINT"]),
        model_deployment=env.get("FOUNDRY_MODEL_DEPLOYMENT", "").strip() or "gpt-5-mini",
        mcp_url=_https_url("MCP_URL", env["MCP_URL"], allow_http_localhost=True),
        dev_customer_id=customer,
        entra=entra,
    )


@dataclass(frozen=True)
class ProductosSettings:
    project_endpoint: str
    model_deployment: str
    search_endpoint: str
    knowledge_base: str

    @property
    def kb_mcp_url(self) -> str:
        """Foundry IQ knowledge base MCP endpoint (preview API, tool knowledge_base_retrieve)."""
        base = self.search_endpoint.rstrip("/")
        return f"{base}/knowledgebases/{self.knowledge_base}/mcp?api-version=2026-08-01-preview"


def load_productos_settings(env: dict[str, str] | None = None) -> ProductosSettings:
    env = os.environ if env is None else env
    missing = [
        name
        for name in ("FOUNDRY_PROJECT_ENDPOINT", "SEARCH_ENDPOINT")
        if not env.get(name, "").strip()
    ]
    if missing:
        raise ConfigError(f"Faltan variables de entorno: {', '.join(missing)}.")
    knowledge_base = env.get("KNOWLEDGE_BASE", "").strip() or "kb-productos"
    if not knowledge_base.replace("-", "").isalnum():
        raise ConfigError("KNOWLEDGE_BASE solo admite letras, números y guiones.")
    return ProductosSettings(
        project_endpoint=_https_url("FOUNDRY_PROJECT_ENDPOINT", env["FOUNDRY_PROJECT_ENDPOINT"]),
        model_deployment=env.get("FOUNDRY_MODEL_DEPLOYMENT", "").strip() or "gpt-5-mini",
        search_endpoint=_https_url("SEARCH_ENDPOINT", env["SEARCH_ENDPOINT"]),
        knowledge_base=knowledge_base,
    )
