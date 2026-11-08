# Diagramas

[`banking-agent.drawio`](banking-agent.drawio) es un archivo de draw.io (XML sin comprimir) con siete páginas. Complementa los diagramas Mermaid de la [guía de aprendizaje](../guia/README.md).

## Cómo abrirlo

| Herramienta | Cómo |
|-------------|------|
| diagrams.net en el navegador | [app.diagrams.net](https://app.diagrams.net) > **File > Open from > Device** y elige el archivo |
| draw.io Desktop | Abre el archivo directamente |
| VS Code | Extensión **Draw.io Integration** (`hediet.vscode-drawio`): abre el `.drawio` como un editor visual |

Las páginas se eligen con las pestañas de la parte inferior.

## Iconos

Los nodos de servicios de Azure usan la biblioteca de iconos de Azure que trae draw.io (`img/lib/azure2/...`, basada en los **Microsoft Azure Architecture Icons**). Se renderizan sin configuración en diagrams.net, draw.io Desktop y la extensión Draw.io Integration de VS Code. Las rutas se verificaron contra el código fuente de draw.io (`jgraph/drawio`, `src/main/webapp/img/lib/azure2/`).

Los límites (suscripción, resource group, Container Apps environment, tenants) son contenedores con borde punteado y un icono pequeño en la esquina. Los componentes que no son recursos de Azure (agentes MAF, middleware, piezas de código) son rectángulos redondeados:

| Color | Significado |
|-------|-------------|
| Azul | Agente o componente de MAF |
| Amarillo | Código propio (validación, resiliencia, verificaciones) |
| Verde | Control que protege o instrumenta |
| Violeta | Datos |
| Gris | Nota o leyenda |
| Rojo | Comportamiento ante falla o bloqueo |

Cada flecha indica el protocolo y la autenticación (por ejemplo, "MCP streamable HTTP, Bearer OBO" o "TDS 1433 · MI"). Las flechas punteadas son dependencias secundarias (identidad, telemetría, configuración).

## Páginas

| Página | Qué muestra |
|--------|-------------|
| 1. Arquitectura general | Todos los recursos agrupados por suscripción, resource group, Container Apps environment y Foundry; el tenant External ID con sus apps; GitHub; el cliente y la CLI |
| 2. Identidad y tokens | Los dos tenants, las app registrations, scopes y app roles, managed identities, Key Vault con el certificado OBO, la credencial federada de GitHub y una tabla de tokens y audiencias |
| 3. Red y conectividad | Ingress externo e interno con puertos, quién llama a quién, servicios PaaS y el bloqueo del acceso directo al MCP |
| 4. Flujo de una conversación | "Quiero bloquear mi tarjeta" en 15 pasos numerados, desde el device code hasta la aprobación y la escritura idempotente |
| 5. A2A y trazas | Cliente A2A en el host (proxy, breaker, `BearerAuth`, HTTPX) y servidor en `productos-a2a` (card, `RequireToken`, `A2AExecutor`, Starlette), con la traza única en Application Insights |
| 6. Modelo de datos | Las 9 tablas de `sqldb-banking` con columnas, claves y relaciones |
| 7. CI/CD y evaluaciones | `ci.yml` en cada push, `evals.yml` con OIDC, el runner con MCP local y la publicación en Foundry Evaluations |

## Cómo se generó

El archivo se generó con un script que calcula las coordenadas sobre una cuadrícula y verifica que ninguna figura se solape. Si lo editas a mano en draw.io, guarda como XML sin comprimir (**File > Properties > Compressed** desactivado) para que los cambios se vean en los diffs.
