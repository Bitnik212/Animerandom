# Contributing

The repo uses **GitHub flow**: `main` is the only long-lived branch and is always
releasable. Every change lands through a pull request.

## Branches

Branch from `main`, keep branches short-lived, and name them `<type>/<scope>-<topic>`:

| Type | Use for |
|---|---|
| `feat/` | New behavior |
| `fix/` | Bug fixes |
| `refactor/` | Code changes without behavior changes |
| `docs/` | Documentation only |
| `test/` | Tests only |
| `chore/` | Tooling, CI, dependencies, repo setup |

Scopes match the repo layout: `ingest`, `rec`, `api`, `infra`, `repo`. Examples:
`feat/ingest-vector-matching`, `fix/rec-sequel-order`, `chore/repo-ci`.

If a change depends on another open PR, branch from that PR's branch and set it as the
base (a stacked PR). Once the lower PR is squash-merged, retarget yours to `main`, and
drop the lower PR's original commits by rebasing only your own on top of `main`:

```bash
git fetch origin
git rebase --onto origin/main <lower-pr-branch> <your-branch>
git push --force-with-lease
```

## Commits and PR titles

Use [Conventional Commits](https://www.conventionalcommits.org/) with the scope:

```
feat(ingest): match anime by feature vectors when no id cross-reference exists
fix(rec): keep als: null in the model info
docs(api): document includeAdult on the similar call
```

PRs are **squash-merged**, so the PR title becomes the commit on `main`. Write it in
the same format. Commits inside a branch can be informal.

## Pull requests

- One logical change per PR. Split service changes from unrelated infra changes.
- Fill in the template: what changed, which contracts it touches, how it was tested.
- CI must be green. `.github/workflows/ci.yml` runs each service's job only when that
  service (or what it depends on) changes. The `ci-ok` job always runs and fails if any
  job that ran failed.
- Follow the "Making changes" rules in the root README for contracts: the catalog
  contract, the API contract, the api ↔ rec-engine contract, and the Elasticsearch mapping.
- Delete the branch after merging.

## Checks to run locally

| Service | Commands (from the service directory) |
|---|---|
| ingest-worker | `uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest` |
| rec-engine | `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` |

The service READMEs explain which Postgres and Redis the tests expect.

## Repository settings

The repository owner applies these once in GitHub → Settings:

- **General:** set the default branch to `main`. Allow squash merging only, and turn on
  "Automatically delete head branches".
- **Branches → branch protection for `main`:**
  - require a pull request before merging
  - require the status check `ci-ok` to pass. Require only `ci-ok`, not the service
    jobs, which are skipped when their service is untouched.
  - require linear history
  - block force pushes and deletions
