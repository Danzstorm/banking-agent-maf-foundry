"""Minimal Microsoft Graph client for the external tenant, authenticated with your `az login`."""

import json
import shutil
import subprocess
import urllib.error
import urllib.request

GRAPH = "https://graph.microsoft.com/v1.0"


def token(tenant_id: str) -> str:
    az = shutil.which("az") or "az"
    args = ["account", "get-access-token", "--tenant", tenant_id, "--resource-type", "ms-graph"]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [az, *args, "--query", "accessToken", "-o", "tsv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def call(tenant_token: str, method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(  # noqa: S310 - fixed https Graph base URL
        GRAPH + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Graph {method} {path}: {exc.code} {exc.read().decode()[:500]}") from exc
    return json.loads(raw) if raw else {}
