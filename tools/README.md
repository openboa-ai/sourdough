# Codex employee for the first paper trade

The macOS adapter requires the Codex CLI at `/opt/homebrew/bin/codex` and an
existing ChatGPT subscription login. It does not use an API key. It starts a
separate read-only Codex session with one preauthorized MCP proposal tool.
The tool stages one paper proposal; it has no company or venue database access.
Sourdough owns admission, dispatch, fill observation and posting.

Run `sourdough employee-profile tools/codex_employee.py` before founding and
use the returned profile for the employee. Provision a separate paper journal
with `sourdough bind-paper`, then pass the same adapter to
`sourdough first-duty --employee`. A hold or failed invocation is not a trade.
Deterministic CI does not call the subscription or qualify the real employee.
