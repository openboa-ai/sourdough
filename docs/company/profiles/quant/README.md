# Quant Hedge Fund

[System map](../../../README.md) · [Glossary](../../../glossary.md) · [Design conventions](../../../conventions.md)

Under [Company](../../README.md), the first business profile adds investment semantics to the company. It proposes and admits orders, reconciles the original submission, and reports returns. A company without the profile still runs. A Board Client is not required for any of these operations.

- [Point-in-time research, portfolio targets and strategy deployment](investments/README.md)
- [Bound paper account, risk and execution](trading/README.md)
- [Investment subledger, performance and authorized projections](fund-books/README.md)

## Who judges and who guarantees

These are decisions in a quant fund, not departments to provision. One Agent can hold several duties within its grants. A deterministic program may execute a pinned rule; neither it nor the Agent writes financial truth.

| Fund work | Employee judgment | Guarantee and authoritative record |
| --- | --- | --- |
| Find the next useful job | Investigate an anomaly, serve a due obligation, delegate, or wait; state what finding would change the decision and when to look again. Stop spending on low-value research. | Company work persists claim, decision and next wake; finance reserves and settles actual spend. Neither scheduler nor board supplies a trading answer. |
| Source data and research | Choose a hypothesis, useful data, competing explanations and a falsifier. Reject an apparent edge that disappears after costs. | Investments preserves source, availability time, revisions and missingness. The experiment pins baseline, splits, trials and costs before scoring; corrected or future information cannot enter an earlier decision. |
| Construct a portfolio | Choose desired exposure, horizon and exit/review condition; size down, hedge if permitted, hold or stay flat. No trade quota. | Portfolio checks target version and pending quantity. Risk admits only authorized account, instrument, size and headroom under current limits; stale or uncertain state blocks dependent new exposure. |
| Execute and recover | Choose permitted order terms or cancel the unfilled remainder; never guess whether an order filled. | Trading reserves before send. Venue owns fills. On uncertainty, recover the original order, preserve its reservation and post each observed fill/fee once. No LLM on the dispatch, reconciliation or restart path. |
| Monitor exposure and operating risk | Interpret drift or a cost spike, stop initiating risk, investigate and propose a revised target or method. | System measures exposure and drawdown from reconciled marks, enforces configured stops on new risk, and keeps reconciliation running. It cannot promise a maximum realized loss through a gap. |
| Close books and allocate resources | Explain results, choose which research or strategy is worth funding, propose hiring only for a demonstrated bottleneck. | Accounting posts actuals; finance controls budget; fund books reconstruct positions, NAV, flows and returns from independent records. Neither an Agent's explanation nor its forecast is a posting. |
| Improve the method | Diagnose repeated misses; propose changing a checklist, tool use or improvement procedure. | Agent Learning isolates trials from production, checks sealed evidence and scope, and versions adoption/rollback. No self-granted tools, risk limit, capital or access to evaluator labels. |

**Small deployment:** one supervised company service/store, a bound Agent runtime and an independently observable paper venue. Keep one durable chain: duty → evidence → decision (including hold) → admitted effect if any → reconciled books → next duty. A client reads projections or sends explicit governance commands; closing it changes nothing. Add a separate machine, specialist Agent or strategy process only to solve a measured constraint. See the [scope cut](../../../README.md#delivery-priorities) and [economic trial](../../../acceptance/net-return/spec.md).
