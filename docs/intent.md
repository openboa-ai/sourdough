# Sourdough product intent

Status: Design draft for review, 2026-11-21. No operating, learning or investment result is claimed. [Company capability map](README.md) · [Development lifecycle](engineering/lifecycle.md)

## Business goal and falsifiable hypothesis

Earn positive long-term net return on invested capital within the mandate's risk and spending limits, without a person finding work, approving ordinary decisions or restarting the fund. Count the whole company's costs, not just trading fees. The board sets the mandate and may suspend it; live capital is not authorized by this design.

**Operating hypothesis:** Agents can do the work otherwise done by people at a quant fund, including researching, writing and maintaining deterministic trading programs. Code that places orders without a model call is still the Agent-run company's work. The business test is whether the unattended fund earns net returns. For a proposed change, the question is whether the company's next operating revision improves on its current one, not whether an Agent beats code. Keep the incumbent when a change has no demonstrated benefit. A stronger model may compete as a new Agent; it neither inherits authority nor counts as improvement without evidence.

| Question | Evidence that answers it | What it cannot establish |
| --- | --- | --- |
| Can it operate unattended? | [Founder shift](acceptance/first-shift/spec.md) and a separate [hired-Agent paper run](acceptance/hired-agent-continuity/spec.md): own research, fill, service loss, reconciliation, next judgment. | Good judgment on every occasion or positive return. |
| Does the company get better at improving its work? | [Recursive learning comparison](acceptance/recursive-learning/spec.md): compare a revised improvement procedure with a frozen one on new problems at matched budgets. | A better model, a lucky trade or a larger memory is not evidence of RSI. |
| Does the fund earn money, and is the proposed change better? | [Net-return comparison](acceptance/net-return/spec.md): report absolute company-net results; compare the candidate with the frozen incumbent under the same risk constraints. Cash is an opportunity-cost reference. | A profitable incumbent need not be replaced; a smaller loss is not a profit. Paper is not realized live performance. |

## A company that can work while its board is away

Sourdough operates a company under a standing mandate. [Company](company/README.md) describes continuing purpose, ownership and obligations. [Employee](employee/README.md) describes who takes responsibility and exercises judgment. Company systems implement the former; an Agent realizes the latter. Its first business profile is a quantitative hedge fund whose Agents pursue investment returns. The [common operating systems](README.md#system-hierarchy) owns Agent identity, work, resources, accounting, finance and operations even when the investment profile is absent. A due obligation can start a new shift while the board is offline. Market observations become an additional trigger when the fund profile is installed.

On returning, the board should be able to ask who accepted a duty, what evidence they had, what changed or was deliberately left alone, and who acts next. For the fund specifically: It should be able to find the market evidence an Agent had at decision time, why the company traded or waited, what the venue actually filled, the resulting exposure and costs, and who or what will act next. If an order is still unresolved, the board should see the uncertainty, its effect on available risk and the company's next reconciliation attempt.

## The first proof: one operating day at the fund

One company starts with one founding Agent. Its charter grants access to a synthetic BTC futures market, bounded simulated trading and limited hiring authority. On first activation the Agent reads the mandate and the company's books, identifies work, and chooses an action. A decision to wait includes a reason and a condition that will bring the Agent back. Ordinary work, order submission within the mandate, recovery and subsequent shifts proceed with zero board messages.

The first demonstrable result is one uninterrupted observation: the real founder identifies a question and publishes its own as-of research, uses the result to decide and submit a paper order, and makes a fresh decision on a later shift after a post-fill company crash and automatic reconciliation. A valid decision to wait does not demonstrate the trading branch. Neither a profitable fixture nor a convincing explanation proves economic performance.

## What belongs to the company

The company retains custody of workspaces, shared repositories, strategy revisions, budgets, operational records and delegated access. People or Agents may change assignments. A successor can recover the same assets and obligations from the company even if the original Agent's computer or model session is gone.

The company decides whether a need calls for research, reassignment, a program or a hire. An HR function, a developer and a risk specialist are possible outcomes of those decisions. The first Agent can operate alone. In a larger company, an Agent can have a dedicated computer, collaborate on company code, or supervise a program that trades within its own grant.

## Scope of the first proof

- Run one company locally against one independently observable simulated instrument. Exercise the normal product entry point, real Agent Runtime, risk check, order execution, recovery and board view together.
- Preserve the Agent across sessions only on its original execution profile. Another runtime means another Agent; company records and outstanding obligations survive a handoff. Define the same authority and record boundaries for a later server deployment.
- Record enough evidence now to evaluate later strategy, skill and memory changes. The local MVP subsequently runs company programs against synthetic funds. A dedicated physical computer per Agent, live-money deployment and model-weight training are extensions.
- Binance BTC futures is the target market. Product and account selection, live risk limits, credentials and live activation require a subsequent decision. The first environment has synthetic funds and an isolated simulated venue.

This first proof is necessary but not sufficient. Keep the [broader company acceptance suite](README.md#integration-and-completion), including staffing, custody, strategy programs and client controls. Its coverage is not a prerequisite for starting a bounded paper comparison once the fund can operate safely and close complete books. Do not claim an untested capability, or make management machinery a condition for measuring the business.
