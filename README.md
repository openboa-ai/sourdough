<img src="https://raw.githubusercontent.com/openboa-ai/yogurt/main/design/assets/projects/sourdough.svg" alt="Sourdough project mark" width="96">

# sourdough

put the dough to work

Sourdough is an OpenBoa project combining an industry-neutral **Core Company OS**—mandates, authority, work scheduling, staffing, private agent desks, corporate accounting, checkpoints, and an optional Board Client—with pluggable business domain modules beginning with **Trading** (research, 3-tier execution across `agent_sandbox`, `company_paper`, and Board-approved `company_live`, and cost-inclusive net return).

Start with the [architecture overview](docs/architecture.md) and the [Core Company OS and Trading cleanup spec](docs/engineering/flow-core-domain-cleanup/spec.md) for the two-layer boundary, or the [first Goal](docs/engineering/flow-delivery/goal-01-04.md) for the initial connected Agent trade slice. The [roadmap](docs/engineering/development-roadmap.md) and [work packages](docs/engineering/work-packages.md) track milestones and component ownership.

The Trading domain objective is long-term net return on capital without day-to-day human operation. Organizing a quant fund around Agents is the hypothesis, not the outcome. Start with the [mandate and three independent tests](docs/intent.md).

Start with the [company, Agent and quant hierarchy and operating diagrams](docs/company-agent-quant.md), and [Company](docs/company/README.md) and [Employee](docs/employee/README.md) for the organizational model. Use the [architecture atlas](docs/views.md) to choose a view, the [functional map](docs/capabilities.md) to find a business outcome, or the [ownership map](docs/README.md#system-hierarchy) to find who can change a record. The [glossary](docs/glossary.md) defines shared terms, and the [founder resilience trial](docs/acceptance/first-shift/spec.md) specifies how research, paper execution and crash recovery work together.

See [Contributing](CONTRIBUTING.md) for branch names, pull requests, and the repository checks. Report security issues through [private vulnerability reporting](SECURITY.md).

## UI development with Yogurt

The Board Client web application in [apps/client](apps/client) uses `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` (`0.1.0-beta.13`) for its components, tokens, and composition rules. Its [Yogurt configuration](apps/client/yogurt.config.json) declares the web UI surface.

### Install and build the Board Client

For Web, `apps/client/package.json` pins `@openboa/yogurt` to `./vendor/openboa-yogurt-0.2.0-local.1.tgz`. Pack that exact local archive from a reviewed `openboa-ai/yogurt` checkout and copy it into `apps/client/vendor/` before installing dependencies:

```sh
# From your local yogurt repository checkout:
cd packages/yogurt
npm ci
npm run build
npm pack
cp openboa-yogurt-0.2.0-local.1.tgz <sourdough-repo>/apps/client/vendor/

# From this repository:
cd apps/client
npm ci
npm run typecheck
npm run check:yogurt
npm run build
```

Run the unified company service with the local Board Client web bridge, or connect `client-web` to a standalone `CompanyDaemon` Unix socket, or upgrade a pre-cleanup store with `migrate-store`:

```sh
# Unified service + loopback web bridge:
sourdough run-company <store> --company-id <company-id> --web-port 0

# Standalone daemon + web bridge:
sourdough serve-company-daemon <store> --company-id <company-id> --socket <daemon-socket-path>
sourdough client-web --socket <daemon-socket-path>

# Upgrade a pre-cleanup v1 store to the canonical core + trading schema:
sourdough migrate-store <store>
```
