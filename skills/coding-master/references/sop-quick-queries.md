# SOP: Quick Queries

Lock-free, read-only commands. No workspace lock required.

**`--workspace`** = workspace slot name (env0/env1/env2 or registered name).
**`--repos`** = registered repo name — operates on source path directly, no lock needed.
All quick-* commands accept either `--workspace` or `--repos` (at least one required).

---

## Config Management

```bash
_bash("$D config-list")
_bash("$D config-add repo dolphin git@github.com:user/dolphin.git")
_bash("$D config-add workspace my-app ~/dev/my-app")
_bash("$D config-add env my-app-prod deploy@server:/opt/my-app")
_bash("$D config-set repo dolphin default_branch develop")
_bash("$D config-remove env old-env")
```

---

## quick-status

```bash
_bash("$D quick-status --workspace alfred")
_bash("$D quick-status --repos alfred")                              # repo mode (no lock needed)
_bash("$D quick-status --repos alfred,dolphin")                      # multi-repo
```
Output (`--workspace`): `data.git` (branch, dirty, last_commit), `data.runtime`, `data.project` (test/lint commands), `data.lock` (null or {task, phase, expired}).
Output (`--repos`): `data.repos` (dict by repo name → {path, git, runtime, project}).

**Viewing diffs**: Never bare `git diff`. Use: (1) `quick-status` + `git diff --stat`, (2) `git diff -- <file>` per file, (3) `_get_cached_result_detail(reference_id, scope='skill', limit=20000)` if truncated.

> For full project review/审查, load **Deep Review** SOP instead.

## quick-test

```bash
_bash("$D quick-test --workspace alfred")                            # all tests
_bash("$D quick-test --workspace alfred --path tests/unit/ --lint")  # specific + lint
_bash("$D quick-test --repos alfred")                                # repo mode (no lock needed)
_bash("$D quick-test --repos alfred --path tests/unit/ --lint")      # repo mode + specific + lint
```
Output (`--workspace`): `data.test` (passed, total, output), `data.overall` ("passed"|"failed"), `data.lint` (if `--lint`).
Output (`--repos`): `data.repos` (dict by repo name → {test, overall, lint?}), `data.overall`.

## quick-find

```bash
_bash("$D quick-find --repos alfred --query 'HeartbeatRunner'")                    # single repo
_bash("$D quick-find --repos alfred,dolphin --query 'HeartbeatRunner' --glob '*.py'")  # multi-repo
_bash("$D quick-find --workspace env0 --query 'def test_' --glob '*.py'")          # workspace
```
Output (`--repos`): `data.repos` (dict by repo name → match lines), `data.count`, `data.truncated`.
Output (`--workspace`): `data.matches` (list), `data.count`, `data.truncated`.

## quick-env

```bash
_bash("$D quick-env --env alfred-prod")
_bash("$D quick-env --env alfred-prod --commands \"tail -50 /var/log/app.log\"")
```
Output: `data.modules` (process status, errors, log tail). To fix issues → load **Bugfix Workflow** SOP.
