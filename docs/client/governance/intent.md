# Govern through CEO Direct Line and Board approvals

Design draft · [Board Client](../README.md) · [System map](../../README.md) · [Development lifecycle](../../engineering/lifecycle.md)

Business situation: [Set direction and inspect it](../../capabilities.md#mandate).

## Operating need

A human Board member needs a clear governance boundary to converse 1:1 exclusively with the company's Founder/CEO, review reserved-matter approval briefs (`DecisionQuestion`, mandate amendments, strategy promotions) with full operational context, escalate items to the CEO without inline chat mutation shortcuts, and audit CEO-delegated HR and offboarding custody read-only without supervising routine employee terminations.

## Observable outcome

The Client isolates 1:1 Board chat strictly to the Founder/CEO (`CEO Direct Line`), pairs every reserved-matter approval card with plain-language impact and `"Ask CEO about this item"` escalation, routes every state-mutating Board action through the Universal Human-Language Confirmation Sheet, and surfaces CEO-executed hiring and offboarding custody strictly as a read-only audit ledger (`"Managed by CEO · Board view is read-only"`).

## Scope of discretion

- **Board-vs-CEO 1:1 boundary**: Permit Board 1:1 directional and advisory messaging strictly with the designated CEO (`ceo_agent_id` / `founder_id`). Direct 1:1 Board chat with non-CEO Agents is structurally absent in the UI and rejected fail-closed by the control plane.
- **Advisory chat vs. explicit commands**: Keep `CEO Direct Line` strictly advisory/directional (`send_ceo_message`); never embed inline command execution buttons inside chat messages or pinned Context Attachment Cards.
- **Reserved approvals vs. delegated HR**: Keep `DecisionQuestion` resolution (`submit_decision_answer`), charter/mandate amendments (`amend_charter`), role bindings, and emergency safety controls (`suspend_new_risk`, `resume_new_risk`, `pause_company`, `resume_company`, `revoke_grant`) with the Board; keep routine employee retirement (`retirement.py`, `TERMINATION_SCOPE = "retire-agent"`) strictly CEO-delegated with zero Board `retire_employee` write action.

## Development changes

- [Present the CEO Direct Line and Contextual Ask CEO](ceo-direct-line/spec.md)
- [Present Board approvals, safety gates, and read-only delegated HR custody](approvals-and-custody/spec.md)
