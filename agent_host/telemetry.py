"""OpenTelemetry to Application Insights (ADR-016), shared by the agent host and Productos A2A.

MAF injects `traceparent` only into MCP calls. For A2A the trace is joined by hand: HTTPX is
instrumented on the client (the agent host) and Starlette on the server (Productos), so one
`/chat` request and the Productos work it triggers land in a single distributed trace.

No-op without APPLICATIONINSIGHTS_CONNECTION_STRING (local runs and tests).

Kept lean for the 1 GB/day Log Analytics cap:
- only `agent_host.*` logs are exported. With the default (root logger) the exporter's own
  azure-core HTTP logs at INFO are exported too, and each export logs another one: thousands of
  rows per hour from a feedback loop.
- the probes (/healthz, /readyz) are not traced: one request every few seconds per replica.
- the a2a-sdk internal spans (dozens per SendMessage) are off with
  OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=false, set in Terraform: a2a-sdk reads it at import.
"""

import os

from starlette.applications import Starlette


def configure_telemetry(service_name: str, app: Starlette) -> bool:
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip():
        return False
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)  # cloud_RoleName in App Insights
    # Read when the Starlette instrumentation is imported, hence before the import.
    os.environ.setdefault("OTEL_PYTHON_STARLETTE_EXCLUDED_URLS", "healthz,readyz")
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.starlette import StarletteInstrumentor

    configure_azure_monitor(logger_name="agent_host")
    # configure_azure_monitor already instruments httpx unless a version check skips it
    # silently; instrumenting again is a no-op then, and a guarantee otherwise.
    HTTPXClientInstrumentor().instrument()
    StarletteInstrumentor.instrument_app(app)
    return True
