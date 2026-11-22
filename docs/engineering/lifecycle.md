# Development lifecycle for the Sourdough design set

Status: proposed process for these changes. This page owns the shared handoff. Each [feature](../README.md) provides its intent, scoped change, test oracle and operating signal. These are development controls. Board approval of ordinary Agent work or individual simulated trades is outside this workflow.

## Handoff by stage

This matrix applies to the product intent, first integration and every linked feature/change. Draft origin: product-owner request; drafted with a coding assistant on 2026-11-20. No reviewer acceptance, delivery permission or third-party compliance certification is claimed. The product owner can correct each intent before accepting its spec. Repository protections apply at every stage.

| Stage and practice | Application to each change | Evidence and responsibility |
| --- | --- | --- |
| Plan · capture the desired outcome | Use the feature's intent, affected actors, objective, constraints, excluded scope and observable outcome. | Product owner records the exact reviewed revision and decision or a reason to revise. |
| Design · combine requirements and design | Link one change to its intent. Specify interface, authority, durability, failures, alternatives, NFR and acceptance. Flag incompatible policies before dependent work. | Spec reviewer records accepted revision, open issues, affected owners and allowed scope. The intent/spec pair and this process identify the applicable guidance. |
| Build · plan against the accepted spec | Prepare a scoped implementation plan with entry point, paths, dependency order, highest-risk seam, tests and fallback. Describe any new decision. | Implementation owner attaches the reviewed plan to the change record before dependent code edits. |
| Build · project instructions | Point implementers to project instructions, contribution rules, normal entry points and adjacent accepted contracts. Keep durable requirements in their owning spec. | Record checkout revision and effective guidance in the change record. Use the repository's existing instruction mechanism. |
| Build · reusable skills | Package a repeated procedure only when its inputs, permissions and expected output are known. Test and version the skill independently. | Owner records applied skill versions or that no special skill was required. Financial authority stays server-enforced. |
| Build · parallel work | Let independent owners implement adapter, domain and UI contracts in separate workspaces. Name one owner for the connected outcome. | Each assignment names touched paths, example data and dependency. The integration owner supplies the same-company actual trace. |
| Test · close the feedback loop | Run the spec's positive and adverse cases from normal entry points, compare against independent expected values, inspect failures and rerun on the actual candidate. | Test owner records build, fixture, commands, expectations, observations and unrun branches. UI changes also retain running-screen evidence. |
| Test · continuous evaluation | Re-run affected regression and held-out judgment cases when prompts, model, grants, skills, tools or harness change. Keep baseline and candidate budgets comparable. | CI runs deterministic checks. Run authorized real-runtime evaluations separately if CI lacks consent or budget. Link both records and retain failed attempts. |
| Deploy · independent change review | Request AI-assisted diff review on each candidate PR; keep required independent human review, especially for financial effects, authority, credentials and release controls. | PR owner records findings, dispositions, actual candidate revision and existing required checks. A missed or unavailable reviewer is not a pass. |
| Deploy · enforcement at sensitive actions | Use the effective repository protections and explicit service permissions. Review changes to controls and risky actions at the proper authority boundary. | Record what enforced the decision and its receipt. A prompt or a proposed hook is not proof that enforcement ran. |
| Deploy · delivery and rollback | Qualify the approved build in its scoped environment, check required CI and post-merge behavior. Identify rollback revision, custody of outstanding work and expected observation after rollback. | Delivery owner records actual target, artifact and observed outcome. Publishing, testnet access and live trading require their own authority. |
| Maintain · feed observations back | Track the signal named in each spec. On breach contain the affected scope, retain source evidence, assign investigation and open or amend a linked intent. | Incident owner records actual impact, last good revision, recovery evidence and a regression case. Recurring scans or on-call automation need scoped access and ownership. |

## Review checklist

A feature is ready to plan when a reviewer can reproduce its normal, denied and recovery outcomes from the named inputs and state who owns the open obligation. Before implementing, record the author, date, intent/spec revisions, relevant generation or review inputs, unresolved concerns and the acceptance decision. In implementation and delivery records use exact revisions, reviewer, check results, environment and evidence links. Track time to reviewed intent/spec, late requirement changes, regressions found before delivery, and time from incident to verified recovery without substituting these measures for product acceptance.

Use scoped, bounded development permissions; retain existing protections for releases and credentials. New automation, provider spend, production activation or a paid dependency needs its own authority. A review draft carries no implementation acceptance, CI, provider execution or deployment receipt. All feature verification starts **UNRUN**.

## Change record for each spec

Use the owning domain, feature and unit as the change key. Link intent, spec, implementation, tests and acceptance by their current repository paths. FR and NFR identify the requirements; Normal and Failure identify the independent cases. Refer to the [conventions](../conventions.md) and [glossary](../glossary.md) for canonical names. Cross-feature claims also need the [integrated demonstration](../README.md#integration-and-completion).

| Checkpoint | Minimum entry to retain |
| --- | --- |
| Intent → spec | Product-owner disposition and exact revisions, design inputs and applicable brand/UX, security or financial constraints. Identify any skill and version actually used, or record none. The responsible component named in each spec owns its flagged concern. Record its resolution, owner or blocking dependency. |
| Spec → build plan | Human product owner accepts the scope after consulting the relevant policy or technical owner. Record a read-only planning pass, proposed files, compatibility/migration, boundaries, dependency stubs, first failing test and rollback. Do not edit implementation on a draft. |
| Plan → candidate | Pin instructions for the actual client, reusable skills and approved tools. Isolate parallel sessions; one owner assembles the working product. A worktree, hook or model cannot grant itself new authority. |
| Candidate → release | Separate deterministic checks, held-out model evaluations and system observation. Record exact build, automated and human review, effective enforcement hook or equivalent protected gate, rollback test and deployment destination. A proposed AI review, hook, eval job or delivery pipeline is not an installed control. |
| Release → next intent | Compare the spec's signal with its predeclared expected range on a real observation window. Link the incident, diagnosis, assigned owner, containment, recovery observation and resulting regression or new intent. Preserve unknowns and failed attempts. |

For example, if a recovered fill is posted twice, the ledger owner contains new dependent risk and retains the two company entries and one venue fill. A corrective intent starts with that discrepancy. A fixed build is checked against the duplicate-delivery case, then the same-company crash run, before anyone claims recovery. The board neither manually repairs the books nor triggers the next shift.
