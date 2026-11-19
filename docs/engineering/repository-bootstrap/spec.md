# Repository bootstrap

This revision applies the selected policy for branch names, PR titles and the final squash commit. Intermediate commit messages are unrestricted.

This change gives contributors one short-lived branch per change, a predictable history on main, and secret checks that run before merging. Issue #1 tracks implementation and follow-up work. The first revision covers a repository with a README and no application manifest.

## Contribution rules

1. Target the protected `main` branch with a pull request. Merge by squash using its validated title. Resolve conversations first. Ordinary changes require no blanket approval. Changes to workflow definitions, their validators, ownership, update configuration, and scanner policy require the designated maintainer's review under CODEOWNERS. The owner already has write access.
2. Use `<type>/<kebab-case>` for a topic branch. The lowercase ASCII slug may start with an issue number, such as `fix/42-retry-timeout`. Allowed types are `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `build`, `chore`, `perf`, `style`, and `revert`. Also accept `codex/<kebab-case>` for managed worktrees. Accept Dependabot's generated branch path only on an update PR opened by Dependabot from this repository. Apply the same topic-branch rule to fork PRs.
3. Require `type(scope)!: summary` on the PR title and on the final squash commit on main. Both the scope and the breaking-change marker are optional. Use the same lowercase type list as branch prefixes. The summary must contain a non-whitespace character. For example, `fix(queue): retry interrupted jobs` and `feat(api)!: remove the legacy endpoint` pass. `WIP` and `fix:` fail. The squash commit may include a body and standard `BREAKING CHANGE:` footer. Keep the initial main commit as history. Contributors may freely use `WIP` and fixup commits on topic branches.
4. Run the naming check on new, reopened, synchronized, edited and ready-for-review PRs. Read only the PR title and source branch for PR naming, independently of its commit count and subjects. After a main push, validate the actual new squash subject and report mismatches. A maintainer can edit the proposed squash message at merge time on GitHub Free, so the push check provides detection after merge.

## Files and CI authority

- Add a short contribution guide, a PR template, and a bug-report route that directs security reports to private vulnerability reporting. Add a repository README entry for the available checks. Require owner review of the CI rules, their implementation, and CODEOWNERS itself.
- Add a stack-neutral `.gitignore` for local credentials, environment files, and editor or operating-system artifacts. A nonsecret `.env.example` and future dependency lockfiles must remain trackable. Normalize text line endings with `.gitattributes` and share editor settings with `.editorconfig`.
- Use GitHub-hosted Linux runners. Run ordinary `pull_request` and main-push checks with explicitly read-only permissions, no deployment credentials and no persistent checkout credentials. Treat PR titles, branch names, candidate files and Git metadata as untrusted data. Keep them out of interpolated shell source. A scanner or tool failure must fail the job.
- Pin external actions to full commit SHAs. Pin and verify Gitleaks CLI and actionlint. Scan the repository's initial history and all newly introduced history on subsequent changes, including secrets added and removed within a PR. Redact findings. Candidate scanner configuration, ignore directives and attributes must not silently suppress the required scan. Keep reviewable exceptions scoped to a documented false positive.
- Check workflow syntax and whitespace in the change. Set timeouts for all jobs. Configure Dependabot for weekly grouped GitHub Actions updates with conventional commit messages. Add package ecosystems, builds, formatters and tests when real manifests and commands exist.
- Once successful, failing and recovered PR results have demonstrated stable checks on the expected revision and GitHub App, add the corresponding required checks to the main ruleset. Exercise both same-repository and fork behavior when possible, recording which cases have remote evidence. Keep all PRs, including documentation-only changes, eligible for the required checks.

## Acceptance

| Requirement | Observable outcome |
| --- | --- |
| Main governance | GitHub's effective branch rules show PR-only squash merges, linear history, denied deletion and force pushes, resolved conversations, and an empty bypass list. |
| Names and revisions | Fixtures exercise valid and invalid titles, unrestricted intermediate commit subjects, breaking changes, ordinary and automated branch names, and a main squash message. A real PR transitions from failing on a title change to passing after correction at the same head. |
| Repository hygiene | A synthetic, inactive test credential added then deleted in a fixture fails Gitleaks without exposing the value. Clean history passes. An invalid workflow fails linting. Local ignore tests admit the example file and lockfiles. |
| Enrollment | GitHub records a successful run on the intended commit. Read back the app-bound required checks, then verify a separate PR and the resulting main commit. Record any unverified path in the issue. |

License choice, release publication, production credentials, package publishing, and organization-wide membership policy are separate owner decisions.
