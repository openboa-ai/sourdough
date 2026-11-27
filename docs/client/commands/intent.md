# Request a governance change

Design draft · [Board Client](../README.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

A board member needs to register owner authority (`register_owner`), answer reserved human decisions (`submit_decision_answer`), exercise company-wide or domain-scoped emergency controls (`pause_company` / `resume_company`), interrupt or cancel stuck work (`interrupt_run` / `cancel_work`), trigger WAL checkpoints (`cut_checkpoint`), send 1:1 directional messages strictly to the Founder/CEO (`send_ceo_message`), and—when the Trading domain module is installed—activate or roll back strategy releases (`activate_strategy_release` / `rollback_strategy_deployment`) and promote or demote execution environments (`promote_execution_environment` / `demote_live_environment`) without supervising ordinary work or routine employee terminations.

## Observable outcome

The client submits an explicit authorized request through `ControlledInlineConfirm` (*What changes* vs. *What stays untouched*, consequence note, and collapsed `Technical details`), hands off execution tracking to the inline `CommandOutboxPanel` backed by `/api/local/pending`, and displays the server receipt or an unresolved state across reconnect.

## Scope of discretion

A visible button or optimistic state is not a permission or an applied revision. The control plane authorizes; the domain owner enforces.

Humans access company state and submit company control only through Client. `CompanyDaemon` exposes `12` supported commands (`8` Core Company OS commands + `4` Trading plugin commands registered by `TradingDomainPlugin` and rejected with HTTP `422` when `"trading"` is absent):

- **Core Company OS (`8`):** `register_owner` (`rbac-command-v1`), `submit_decision_answer` (`decision-command-v1`), `send_ceo_message` (`ceo-chat-command-v1`, strictly to `ceo_agent_id`), `pause_company` / `resume_company` (`restriction-command-v1`, with `execution:*` / `trading_risk_pause` gated `422` when `"trading"` is absent), `interrupt_run` / `cancel_work` (`work-command-v1`), and `cut_checkpoint` (`checkpoint-command-v1`). Routine employee retirement and offboarding custody remain CEO-delegated under `retirement.py` and read-only in `/reports` (zero Board `retire_employee` write command).
- **Trading Domain (`4`, gated on `"trading"`):** `activate_strategy_release` / `rollback_strategy_deployment` (`strategy-command-v1`) and `promote_execution_environment` / `demote_live_environment` (`execution-tier-command-v1`), plus `TradingRestrictionGuard` (`<= 15s` quote freshness check) on `execution:*` resume.

Submitted, unknown, accepted, applied, denied, and retry-choice states are distinct. Agent and company operation do not depend on Client.

## Development changes

- [Submit a command and resolve its receipt](submit/spec.md)
