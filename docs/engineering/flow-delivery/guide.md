# Implementation and review guide

Start at the [implementation architecture](../implementation-architecture.md) for dependency direction, transaction boundaries, version compatibility and operating limits. Change the owning spec if a decision changes; do not copy these rules into every unit.

## What blocks a change

1. **Change a decision at its owner.** Fix a derived view at its source and exercise its consumer. A limit that needs independent edits in Risk and the Client has two owners.
2. **Keep the shortest path that preserves the guarantee.** Introduce a boundary to hide a real dependency or enforce a contract. Founding must not import a trading SDK; it does not need a pass-through service for every table either.
3. **Preserve the caller's contract.** A new adapter must pass the shared cases for success, rejection, unknown outcomes and replay where supported. Report unsupported operations explicitly. A timeout is not a rejection.
4. **Make each effect traceable.** Check authority at use; commit related local facts and their receipt together. Keep external waits outside that transaction. Replay the same identity only for matching terms; resolve an uncertain external effect before sending again.
5. **Keep the judge independent.** Test the public entry point against separately observed effects. Tests can ship with code; skipping a failing case or relaxing its expected outcome to pass is not a fix. Change the owning spec explicitly if the contract itself must change.
6. **Leave a reproducible handoff.** In the PR's existing Verification section, give the tested commit, command, expected and observed result, and anything not verified. A blocking finding must name the broken promise or concrete maintenance cost. Recheck the affected behavior on the final head, not settled style preferences or unrelated defects.

**Apply checks by risk.** Block authority bypass, wrong destinations, wrong amounts or units, duplicate effects, data loss and weakened acceptance from first use. Check an old store on a format change and check regeneration against its source when generated outputs are introduced. Turn recurring defects into the cheapest check that rejects a known bad change and accepts a sound one. Tune or remove noisy checks without dropping coverage of a required outcome.

**First use — [founding](../../company/systems/organization/found-company/bootstrap/spec.md).** Lose the reply after commit and repeat setup. Independently reopen the store: there must be one company, founder, grant and first duty, each identified by the receipt. Returning the old receipt while inserting another duty must fail. Changing the terms under the same request ID must be rejected without changing those records. A private-helper rename alone must not break the test. This proves founding, not a trade.

## Implementation defaults

**Stack.** Start with one Python package under `src/sourdough/` and a transactional SQLite store. The company owns identity, grants and work; the fund owns paper accounts, orders and positions. Neither the Agent nor the Client writes directly to the books. Keep the trading SDK behind the company-managed execution adapter; use Yogurt only in the Client build. Separate the macOS supervisor when implementing recovery, not as a prerequisite for the first trade.

**First package.** Pin a tested CPython patch in `.python-version`. Declare the package, entry point and build backend in `pyproject.toml`. Pin Ruff, the backend and other CI tools with hashes in `requirements-dev.lock`. Use the standard library for founding. Add modules with their first use; do not create a service per spec.

**Code.** Use glossary terms, `snake_case` for Python modules/functions and `PascalCase` for types. Type public commands and results, validate input at the adapter boundary, give each business action a stable ID and represent amounts with units and explicit precision.

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
