# Govern through Board approvals, controls, and CEO dialogue

Design draft · [Board Client](../README.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

A human Board member needs a clear governance boundary to converse 1:1 exclusively with the company's Founder/CEO (`/messages`), review reserved-matter approval briefs (`DecisionQuestion` on `/approvals` and contextually via `ContextualApprovalCard`), escalate items to the CEO without inline chat mutation shortcuts, exercise scoped operational/trading controls (`/controls`), and audit CEO-delegated HR and offboarding custody read-only (`/reports`) without supervising routine employee terminations.

## Observable outcome

The Client isolates 1:1 writable Board chat strictly to the Founder/CEO (`/messages`, `Founder · CEO`), pairs every reserved-matter approval card with plain-language impact and `"Ask CEO about this item"` escalation, routes every state-mutating Board action through `ControlledInlineConfirm`, and surfaces CEO-executed hiring and offboarding custody strictly as a read-only audit ledger on `/reports` (`"Managed by CEO · Board view is read-only"`).

## Scope of discretion

- **Board-vs-CEO 1:1 boundary**: Permit Board 1:1 directional and advisory messaging strictly with the designated CEO (`ceo_agent_id` / `founder_id`). Direct 1:1 Board chat with non-CEO Agents is structurally absent in the UI and rejected fail-closed by the control plane.
- **Advisory chat vs. explicit commands**: Keep `/messages` strictly advisory/directional (`send_ceo_message`); never embed inline command execution buttons inside chat messages or pinned `ContextAttachmentCard`s.
- **Reserved approvals vs. delegated HR**: Keep `DecisionQuestion` resolution (`submit_decision_answer` including `live_promotion` with `application_status = 'failed'` handling), emergency safety controls (`pause_company`, `resume_company`, `interrupt_run`, `cancel_work`), and Trading domain promotions/deployments (`promote_execution_environment`, `demote_live_environment`, `activate_strategy_release`, `rollback_strategy_deployment` when `"trading"` is installed) with the Board; keep routine employee retirement (`retirement.py`, `TERMINATION_SCOPE = "retire-agent"`) strictly CEO-delegated with zero Board `retire_employee` write action.

## Development changes

- [Present Company Messages and Contextual Ask CEO](ceo-direct-line/spec.md)
- [Present Board approvals, safety gates, and read-only delegated HR custody](approvals-and-custody/spec.md)
