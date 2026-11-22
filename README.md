# sourdough

put the dough to work

Sourdough is an OpenBoa project to build a quantitative fund that AI agents can operate under a standing mandate. It explores whether agents can run research, paper trading and day-to-day operations without a person directing each step.

개발 시작점: [전체 실행계획과 Terra 인계](docs/engineering/development-roadmap.md) · [작업별 소유 명세](docs/engineering/work-packages.md). 첫 목표는 수익 최적화가 아니라 모의 체결 직후 중단된 회사가 스스로 다음 근무까지 이어 가는 것이다.

The business goal is long-term net return on capital without day-to-day human operation. Organizing a quant fund around Agents is the hypothesis, not the outcome. Start with the [mandate and three independent tests](docs/intent.md).

Start with the [company, Agent and quant hierarchy and operating diagrams](docs/company-agent-quant.md), and [Company](docs/company/README.md) and [Employee](docs/employee/README.md) for the organizational model. Use the [architecture atlas](docs/views.md) to choose a view, the [functional map](docs/capabilities.md) to find a business outcome, or the [ownership map](docs/README.md#system-hierarchy) to find who can change a record. The [glossary](docs/glossary.md) defines shared terms, and the [first unattended shift](docs/acceptance/first-shift/spec.md) specifies how research, paper execution and crash recovery must work together.

See [Contributing](CONTRIBUTING.md) for branch names, pull requests, and the repository checks. Report security issues through [private vulnerability reporting](SECURITY.md).
