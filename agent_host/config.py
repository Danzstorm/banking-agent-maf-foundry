"""Settings from environment variables. No secrets: Foundry auth comes from Entra (ADR-008)."""

import os
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigError(Exception):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    project_endpoint: str
    model_deployment: str
    mcp_url: str
    dev_customer_id: int


def _https_url(name: str, value: str, *, allow_http_localhost: bool = False) -> str:
    value = value.strip()
    url = urlparse(value)
    local = url.hostname in {"127.0.0.1", "localhost"}
    if url.scheme == "https" or (allow_http_localhost and url.scheme == "http" and local):
        return value
    raise ConfigError(f"{name} debe ser una URL https.")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    missing = [
        name
        for name in ("FOUNDRY_PROJECT_ENDPOINT", "MCP_URL", "DEV_CUSTOMER_ID")
        if not env.get(name, "").strip()
    ]
    if missing:
        raise ConfigError(f"Faltan variables de entorno: {', '.join(missing)}.")
    raw_customer = env["DEV_CUSTOMER_ID"].strip()
    if not (raw_customer.isascii() and raw_customer.isdigit() and int(raw_customer) > 0):
        raise ConfigError("DEV_CUSTOMER_ID debe ser un entero positivo.")
    return Settings(
        project_endpoint=_https_url("FOUNDRY_PROJECT_ENDPOINT", env["FOUNDRY_PROJECT_ENDPOINT"]),
        model_deployment=env.get("FOUNDRY_MODEL_DEPLOYMENT", "").strip() or "gpt-5-mini",
        mcp_url=_https_url("MCP_URL", env["MCP_URL"], allow_http_localhost=True),
        dev_customer_id=int(raw_customer),
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
