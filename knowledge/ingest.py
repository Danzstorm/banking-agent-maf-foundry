"""Create the Foundry IQ blob knowledge source and knowledge base, then wait for indexing.

    export SEARCH_ENDPOINT=$(terraform -chdir=infra output -raw search_endpoint)
    export STORAGE_ACCOUNT_ID=$(terraform -chdir=infra output -raw storage_account_id)
    export FOUNDRY_OPENAI_ENDPOINT=$(terraform -chdir=infra output -raw foundry_openai_endpoint)
    uv run python knowledge/ingest.py            # create (or reuse) and wait
    uv run python knowledge/ingest.py --reindex  # after publishing new PDFs

Idempotent: an existing knowledge source is reused (its generated indexer is re-run only with
--reindex) and the knowledge base is created-or-updated with PUT. Auth is Entra ID only; your
az login needs Search Service Contributor and Search Index Data Contributor.
"""

import argparse
import os
import sys
import time

import httpx
from azure.identity import DefaultAzureCredential

API_VERSION = "2026-04-01"  # GA: blob knowledge source + extractive retrieval
# The KB MCP endpoint is preview and, without a KB model, only accepts minimal reasoning effort,
# which only the preview API can store on the knowledge base.
PREVIEW_API_VERSION = "2026-08-01-preview"
SCOPE = "https://search.azure.com/.default"
CONTAINER = "productos"
EMBEDDING_MODEL = "text-embedding-3-small"


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, "").strip() or default
    if not value:
        sys.exit(f"Falta la variable de entorno {name}.")
    return value


def knowledge_source_body(
    name: str, storage_account_id: str, openai_endpoint: str, deployment: str
) -> dict:
    return {
        "name": name,
        "kind": "azureBlob",
        "description": "Documentos de productos de Banco Andino Demo (ficticios), en PDF.",
        "azureBlobParameters": {
            # ResourceId connection string: the search service's managed identity reads Blob.
            "connectionString": f"ResourceId={storage_account_id};",
            "containerName": CONTAINER,
            "isADLSGen2": False,
            "ingestionParameters": {
                "contentExtractionMode": "minimal",
                "disableImageVerbalization": True,  # text-only PDFs: no chat model needed
                "embeddingModel": {
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        # No apiKey/authIdentity: the system managed identity is used.
                        "resourceUri": openai_endpoint,
                        "deploymentId": deployment,
                        "modelName": EMBEDDING_MODEL,
                    },
                },
            },
        },
    }


def knowledge_base_body(name: str, knowledge_source: str) -> dict:
    return {
        "name": name,
        "description": "Productos, tarifas y procesos de Banco Andino Demo (ficticio).",
        "knowledgeSources": [{"name": knowledge_source}],
        # No LLM in the knowledge base: extractive retrieval, our agent synthesizes (ADR-012).
        "retrievalReasoningEffort": {"kind": "minimal"},
        "outputMode": "extractiveData",
    }


class Search:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.credential = DefaultAzureCredential()
        self.client = httpx.Client(timeout=60)

    def request(
        self, method: str, path: str, body: dict | None = None, api_version: str = API_VERSION
    ) -> httpx.Response:
        token = self.credential.get_token(SCOPE).token
        resp = self.client.request(
            method,
            f"{self.endpoint}/{path}",
            params={"api-version": api_version},
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            sys.exit(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp


def wait_for_sync(search: Search, ks: str, timeout_s: int = 900) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        status = search.request("GET", f"knowledgesources/{ks}/status").json()
        current = status.get("currentSynchronizationState")
        last = status.get("lastSynchronizationState") or {}
        if current:
            print(f"  indexando: {current.get('itemsUpdatesProcessed', 0)} procesados")
        elif last.get("endTime"):
            return status
        else:
            print("  en cola...")
        if time.monotonic() > deadline:
            sys.exit("Tiempo de espera agotado; revisa el indexer en el portal.")
        time.sleep(15)


def main() -> int:
    parser = argparse.ArgumentParser(description="Crea la knowledge base de productos.")
    parser.add_argument("--reindex", action="store_true", help="Re-ejecuta el indexer.")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

    search = Search(env("SEARCH_ENDPOINT"))
    ks = env("KNOWLEDGE_SOURCE", "ks-productos")
    kb = env("KNOWLEDGE_BASE", "kb-productos")

    existing = search.request("GET", f"knowledgesources/{ks}")
    if existing.status_code == 404:
        body = knowledge_source_body(
            ks,
            env("STORAGE_ACCOUNT_ID"),
            env("FOUNDRY_OPENAI_ENDPOINT"),
            env("EMBEDDING_DEPLOYMENT", EMBEDDING_MODEL),
        )
        created = search.request("PUT", f"knowledgesources/{ks}", body).json()
        print(f"knowledge source {ks} creada")
        resources = created["azureBlobParameters"].get("createdResources") or {}
    else:
        print(f"knowledge source {ks} ya existe; se reutiliza")
        resources = existing.json()["azureBlobParameters"].get("createdResources") or {}
        if args.reindex:
            search.request("POST", f"indexers/{resources['indexer']}/run")
            print(f"indexer {resources['indexer']} re-ejecutado")
    for kind, name in resources.items():
        print(f"  {kind}: {name}")

    search.request("PUT", f"knowledgebases/{kb}", knowledge_base_body(kb, ks), PREVIEW_API_VERSION)
    print(f"knowledge base {kb} creada o actualizada")

    if args.reindex:
        time.sleep(5)  # let the run register before polling
    status = wait_for_sync(search, ks)
    last = status["lastSynchronizationState"]
    print(
        f"sincronización terminada {last['endTime']} | procesados "
        f"{last.get('itemsUpdatesProcessed')} | fallidos {last.get('itemsUpdatesFailed')}"
    )
    for error in last.get("errors") or []:
        print(f"  error: {error.get('docURL')}: {error.get('errorMessage')}")
    print(f"MCP: {search.endpoint}/knowledgebases/{kb}/mcp?api-version={PREVIEW_API_VERSION}")
    return 0 if not last.get("itemsUpdatesFailed") else 1


if __name__ == "__main__":
    sys.exit(main())
