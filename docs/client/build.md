# Board Client build contract

The Board Client is a React 19 web presentation application built with `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` (`0.1.0-beta.13`), replacing the superseded `apps/board/` v0 native Zig (`0.16.0`) / Native SDK (`0.11.1`) GPU-surface target. Pin the React UI dependencies and design-system bundle solely in the Client build manifest and lockfile, never in the Python company service. The UI package supplies presentation; `CompanyDaemon` supplies the local Unix domain socket (UDS) Control Plane; neither is the operating system of the company or a prerequisite for the first Agent paper trade.

**`@openboa/yogurt` is an upstream design system.** Install and import its released web UI assets (`--target web`) into the Sourdough Board Client. It is not the client itself, a runtime service, a data provider, or part of the company and quant system model. The compiled client owns its behavior and bundles the components, CSS `@layer` styles, and tokens it uses. No company operation or read API needs a connection to the package publisher.

## Visual and accessibility baseline

Pin and preserve the `@openboa/yogurt` `0.2.0-local.1` presentation contract inside the client build:

- **Palette & Signal Discipline**: Use the `21 x 11` (`231`-color) palette with quiet warm-neutral/obsidian surfaces and monochrome `--yg-inverse` for everyday controls (`Button`, `Switch`, `Checkbox`, `Radio`, `Toggle`, `ToggleGroup`, `Tabs`, `Toast`). Reserve OpenBoa Ink Blue (`#175174` with `#FFFFFF` text) exclusively for explicit human commitment CTAs (`Confirm` actions in governance dialogs, emergency stop/resume confirmation, strategy activation/rollback). Use readable `--yg-brand-ink` for links, focus rings, progress fills, and chart series strokes.
- **Typography & Concentric Geometry**: Use `Rethink Sans` with `tabular-nums` for all numeric amounts, quantities, revisions, timestamps, and sequence numbers. Restrict radii strictly to `{4, 6, 8, 12, 16, 20, 999}px`, use `1px` alpha hairlines, and enforce `0` CSS gradients and `0` static card box-shadows (elevation shadows belong only to floating overlays such as dialogs, sheets, menus, and popovers).
- **Agent & Accessibility Controls**: Expose descriptive `data-agent-confirm` attributes solely on explicit confirmation triggers inside `ControlledConfirmDialog` (built on `@ui-sdk/react/button`), `data-agent-hidden` on redacted/host-locked DOM, `data-agent-secret` on sensitive credential handles if ever rendered, and `createYogurtAgent` integration only over read/confirmation boundaries that require explicit human confirmation (`details?.source !== "agent"`).

## Host topologies and `apps/board/` disposition

Because `@openboa/yogurt` `0.2.0-local.1` targets `--target web` exclusively (`yogurt.mjs` rejects `--target native`) and `apps/board/` v0 (`app.native`, `prepare-ui.sh --target native`, `main.zig` `.gpu_surface`, per-click `sourdough client-bridge` subprocesses) lacks command submission, live event streaming, and `(company_id, principal_id)` pending isolation, the React 19 bundle supports two host topologies defined in [Client architecture](architecture.md):

1. **Primary standalone host (`sourdough client-web`)**: Serve the compiled React 19 bundle behind an authenticated local loopback HTTP-to-UDS bridge (`127.0.0.1:<ephemeral>`) communicating solely with `CompanyDaemon` over its `0o600` UDS socket (`/v1/*`).
2. **Probe-gated desktop host (`zero://app` WebView)**: Any reuse of `apps/board/` (`Native SDK 0.11.1`) as a desktop WebView shell loading the compiled React 19 bundle from `zero://app` is gated on passing a pre-implementation Native SDK `0.11.1` capability probe verifying system WebView rendering, `zero://app` CSP, and bounded JS-to-Zig IPC that preserves `pending.zig`'s `0o600` `O_NOFOLLOW` `board-pending` persistence and UDS isolation.

## Build and verification rules

1. At implementation, verify the upstream `@openboa/yogurt` (`0.2.0-local.1`) and `@ui-sdk/react` (`0.1.0-beta.13`) distributions, immutable revisions, supported public exports, asset licenses, and integrity. Pin them in the client `package.json` and lockfile. Do not invent a registry name, package command, or component API.
2. Only the client presentation layer imports UI assets. Keep work and financial data, access rules, authority checks, and command schemas in their owning Python/SQLite contracts. No operational Python package imports the client or its Node/UI dependencies.
3. Verify a clean production build using the pinned artifacts. Exercise authorized queries, NDJSON `/v1/events` streaming, and explicit command/receipt cycles against the live local `CompanyDaemon` UDS service, not only a static fixture. Capture the running client's keyboard paths, contrast, and visual states for `idle`, `submitting`, `pending`, `denied` (`conflicting_revision`), `stale`, `unknown`, `applied`, `not_found_retry_choice`, and `host_locked`. Re-test after upgrades; use an older client only with a compatible `control-plane-v1` service contract (`GET /v1/meta`).
4. If a release or required export is unavailable, record a **client-build** gap. Do not install a substitute into the company service, invent an upstream API, or call a mock screen a verified client. Headless operating and recovery tests remain independent.

[Client architecture](architecture.md) · [Board Client responsibilities](README.md) · [Dependency rules](../conventions.md)
