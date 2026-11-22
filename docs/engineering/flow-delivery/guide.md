# Implementation and review guide

Start at the [implementation architecture](../implementation-architecture.md) for dependency direction, transaction boundaries, version compatibility and operating limits. Change the owning spec if a decision changes; do not copy these rules into every unit.

## Keeping a change maintainable

- **Localize the next edit.** Put a rule where its owner changes it. Fix derived views at their source and check the consumer's result. Keep a direct call unless a boundary hides a real dependency or enforces a guarantee; similar-looking code with different policies need not share a helper.
- **Preserve meaning across implementations.** An adapter may change the transport, not what its caller can infer. An unanswered order is not a rejection. Run the same relevant contract cases against each supported adapter; declare unsupported operations instead of silently approximating them.
- **Make effects easy to trace.** Keep the authority check, state transition and commit boundary visible. Commit related local facts together; never wait for an external call inside that transaction. Distinguish a safe replay from a new request with conflicting terms.
- **Test what the next consumer sees.** Exercise the public entry point and inspect its result independently, not its helper-call sequence. For [founding](../../company/systems/organization/found-company/bootstrap/spec.md), lose the reply after commit and repeat setup: the receipt must identify the same company, founder, grant and duty, with no new records. Renaming a private helper must not break that test.

Apply only the criteria the change touches. Block a broken guarantee or needless coupling, not a preference for another directory layout or design pattern.

**Stack.** Start with one Python package under `src/sourdough/` and a transactional SQLite store. The company owns identity, grants and work; the fund owns paper accounts, orders and positions. Neither the Agent nor the Client writes directly to the books. Keep the trading SDK behind the company-managed execution adapter; use Yogurt only in the Client build. Separate the macOS supervisor when implementing recovery, not as a prerequisite for the first trade.

**First package.** Pin a tested CPython patch in `.python-version`. Declare the package, entry point and build backend in `pyproject.toml`. Pin Ruff, the backend and other CI tools with hashes in `requirements-dev.lock`. Use the standard library for founding. Add modules with their first use; do not create a service per spec.

**Code.** Use glossary terms, `snake_case` for Python modules/functions and `PascalCase` for types. Type public commands and results; distinguish rejected, unknown and confirmed effects. Validate input at the adapter boundary. Give each record one writer and each business action a stable ID. Represent amounts with units and explicit precision; never resubmit an order just because its result is unknown.

**Branches and handoff.** Follow [Contributing](../../../CONTRIBUTING.md): `<type>/<kebab-slug>` for topics (e.g. `feat/7-book-paper-fill`), `codex/<kebab-slug>` for managed worktrees, and conventional PR titles such as `feat(trading): book the first paper fill`. Base a unit PR on the current tip of `feat/flow-01-04`, not on `main`. Fix review findings on the same PR; recheck the new head. Only the same-repository aggregate goes from `feat/flow-<slug>` to `main` for product delivery. An automation must honor the same branch, target and title contract or leave publication to the normal contributor path.

## Automatic merge

As part of opening an authorized, ready PR, request automatic squash merge for its exact head:

```sh
gh pr merge "$PR" --auto --squash --match-head-commit "$PR_HEAD_SHA"
```

Do not request optional human reviews by default. Required reviews and CI still apply; never use `--admin`. For an aggregate product PR, attach and verify the Goal's required actual-run evidence for that head **before** requesting merge. Keep auto-merge off while evidence is missing or a replacement head is being qualified.

Read back that auto-merge is enabled or the PR is already merged. If GitHub blocks the request, report the exact missing condition. After merge, check the destination revision and its applicable CI.

**Status.** In the existing flow Issue or PR, record only what changed. Keep these facts separate:

| Fact | What to record |
| --- | --- |
| Design and admission | Draft / changes requested / accepted for a specific spec revision; implementation not admitted / admitted for a particular base and scope. |
| Work | Working, waiting for CI, waiting for review, or needs decision. Name the next action and owner. A label classifies work; it never grants authority. |
| Verification | `NOT_RUN` (also written `UNRUN` in earlier records), `BLOCKED` with a missing prerequisite, `FAIL` with an observed mismatch, or `PASS` with candidate revision, environment and evidence. A new head needs new evidence. |
| Delivery | Open PR, merged into flow, or merged into main; read the actual destination SHA. A merge is not a product run. |

For example: “Unit PR open; waiting for review. Candidate unit tests PASS. Actual Agent paper fill NOT_RUN. Next: run the joined candidate.” No extra status file, board or label setup is required.

Optional Issue classification: one of `bug`, `enhancement`, `documentation`, `maintenance`; at most one of `priority:low`, `priority:medium`, `priority:high` or `priority:urgent`. `security` and `dependencies` are facets. `needs:triage` means classification is unresolved; `needs:approval` means an actual decision is pending. Do not infer execution state from these labels or require installing them before development.

**Checks.** Run the following in a clean environment. Fast tests cover the changed transition and its immediate consumer; a flow PR additionally runs its named end-to-end suite on macOS.

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps .
python -m ruff format --check src tests
python -m ruff check src tests
python -I .github/scripts/run_required_tests.py tests/unit
```

**Review.** Who is permitted to act? Which record changed, and can the next consumer read the correct result? Can a retry duplicate an effect? Block missing authority, wrong destinations, false evidence and lost or duplicate postings. Leave unrelated naming preferences, future abstractions and later qualification work out of the feature PR. Verify required checks and owner reviews on the final head; never bypass branch protection.
