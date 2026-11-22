# Unit PRs and flow integration

Owner: repository CI. This extends the [repository bootstrap](../repository-bootstrap/spec.md). It separates fast review of small changes from acceptance of a working product flow.

| Route | Required observation |
| --- | --- |
| Topic → `feat/flow-<slug>` | Naming, hygiene, package formatting, lint and fast product behavior on the merge candidate. Do not run paid models or long operating trials on every PR. |
| Same-repository `feat/flow-<slug>` → `main` | Run the fast checks and that flow's macOS suite. Require `test_flow.ConnectedFlow.test_end_to_end` in `tests/flows/flow-<slug>`. An unrelated pass, missing case, empty suite, skip or failure cannot pass. The integration owner also checks the Goal's actual-run evidence against this candidate before merging; hosted CI alone does not prove an actual Agent trade. |
| Other topic → `main` | Documentation and repository maintenance only. Route product additions, edits, renames and deletions through the flow branch. |
| Push to `main` | Check the resulting squash subject and repository hygiene. Keep the product evidence linked to the merged candidate. |

`Required CI` checks all applicable results. PR jobs have read-only access, no trading or model credentials, and no permission to change hosting rules. Treat branch names and PR titles as data. Check the unit PR's results and applicable owner reviews before squash; a workflow trigger is not itself branch protection.

Verification covers direct and aggregate routes, forks, malformed branch names, missing and skipped tests, failures and unrelated passing tests. To revert the policy, pause new unit merges, preserve incomplete flows outside `main` and restore the previous policy through a protected PR.
