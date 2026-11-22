# Board Client build contract

The Board Client is a native macOS desktop application built with the Vercel Native SDK. Pin the SDK and a compatible Yogurt native release in the Client build, not in the Python company service. The SDK is the desktop host; Yogurt supplies presentation. Neither is the operating system of the company or a prerequisite for the first Agent paper trade.

**Yogurt is an upstream design system.** Install and import its released UI assets into the Sourdough Board Client. It is not the client itself, a runtime service, a data provider, or part of the company and quant system model. The compiled client owns its behavior and may bundle the components, styles and tokens it uses. No company operation or read API needs a connection to the package publisher.

1. At implementation, verify the upstream distribution, version or immutable revision, supported public exports, asset licenses and integrity. Pin it in the client manifest and lockfile (or the equivalent for its supported packaging). Do not invent a registry name, package command or component API.
2. Only the client presentation layer imports the supported assets. Keep work and financial data, access rules, authority checks and command schemas in their owning contracts. No operational package imports the client or its UI dependencies.
3. Verify a clean native build using the pinned artifacts. Exercise one authorized query and one explicit command/receipt against the local service, not only a UI fixture. Capture the running client's keyboard and visual states for pending, denied, stale, unknown and applied receipts. Re-test after upgrades; use an older client only with a compatible service contract.
4. If a release or required export is unavailable, record a **client-build** gap. Do not install a substitute into the company service, invent an upstream API or call a mock screen a verified client. Headless operating and recovery tests remain independent.

[Board Client responsibilities](README.md) · [Dependency rules](../conventions.md)
