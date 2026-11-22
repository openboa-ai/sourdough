# Codex employee for the first paper trade

The macOS adapter requires the Codex CLI at `/opt/homebrew/bin/codex` and an
app-specific ChatGPT subscription login. It does not use an API key or reuse
the default development Codex login. Connect the Sourdough profile once:

```sh
export CODEX_HOME="$HOME/Library/Application Support/Sourdough/Codex"
mkdir -p -m 700 "$CODEX_HOME"
codex -c 'cli_auth_credentials_store="file"' login
```

The employee process uses an application-specific home and a temporary
working directory, ignores user Codex configuration, and supplies the
paper tool explicitly instead of inheriting development MCP settings.
The adapter starts a
separate read-only Codex session with one preauthorized MCP proposal tool.
The tool stages one paper proposal; it has no company or venue database access.
Sourdough owns admission, dispatch, fill observation and posting.

Run `sourdough employee-profile tools/codex_employee.py --sandbox local-macos`
before founding and use the returned profile for the employee. The backend is
part of the immutable profile; this adapter does not fall back to a hosted
environment. Provision a separate paper journal
with `sourdough bind-paper`, then pass the same adapter to
`sourdough first-duty --employee`. A hold or failed invocation is not a trade.
Deterministic CI does not call the subscription or qualify the real employee.
