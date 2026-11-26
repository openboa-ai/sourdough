<img src="https://raw.githubusercontent.com/openboa-ai/yogurt/main/design/assets/projects/sourdough.svg" alt="Sourdough project mark" width="96">

# sourdough

put the dough to work

Sourdough is an OpenBoa project to build a quantitative fund that AI agents can operate under a standing mandate. It explores whether agents can run research, paper trading and day-to-day operations without a person directing each step.

Start with the [first Goal](docs/engineering/flow-delivery/goal-01-04.md): build the connected company, Agent and paper trading path until the Agent's first order fills and the resulting position is booked. The [roadmap](docs/engineering/development-roadmap.md) and [work packages](docs/engineering/work-packages.md) describe subsequent milestones.

The business goal is long-term net return on capital without day-to-day human operation. Organizing a quant fund around Agents is the hypothesis, not the outcome. Start with the [mandate and three independent tests](docs/intent.md).

Start with the [company, Agent and quant hierarchy and operating diagrams](docs/company-agent-quant.md), and [Company](docs/company/README.md) and [Employee](docs/employee/README.md) for the organizational model. Use the [architecture atlas](docs/views.md) to choose a view, the [functional map](docs/capabilities.md) to find a business outcome, or the [ownership map](docs/README.md#system-hierarchy) to find who can change a record. The [glossary](docs/glossary.md) defines shared terms, and the later [founder resilience trial](docs/acceptance/first-shift/spec.md) specifies how research, paper execution and crash recovery must work together.

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
npx --no-install yogurt check
npm run build
```

Serve the built client locally through the authenticated loopback bridge connected to a running `CompanyDaemon` Unix socket:

```sh
sourdough client-web --socket <daemon-socket-path>
```
