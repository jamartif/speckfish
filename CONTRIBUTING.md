# Contributing

## Everything goes through a pull request

`main` is protected: no direct pushes. The flow is always the same.

```bash
git switch -c feat/short-description        # or fix/, docs/, chore/, ci/
# ... changes ...
uv run --with pytest python -m pytest -q    # or: .venv/Scripts/python -m pytest -q
ruff check src tests
git add -A && git commit -m "feat: short description"
git push -u origin HEAD
gh pr create --fill                          # or write the body yourself
```

Then wait for the checks, and merge with **squash** so `main` keeps one commit per change:

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

## What CI enforces on every PR

| Check | Workflow | Failing means |
|---|---|---|
| `lint` (`ruff check src tests`) | `pr.yml` | fix the findings, do not silence the rule |
| tests on Ubuntu + Windows (3.10, 3.12) | `tests.yml` | a real regression; the suite needs no network and no agent CLI |
| PR title format | `pr.yml` | the title is not `type: description` (Conventional Commits) |

The PR also gets `type` and `size/*` labels automatically, and reviewers are requested from
`.github/CODEOWNERS` (plus the repository variable `REVIEWERS` — comma separated logins — when set).
macOS runs separately (`tests-macos.yml`, weekly + tags + manual) because shared macOS runners are
scarce.

## Commit and PR titles

Conventional Commits, one intent per PR:

```
feat: add the constitution subcommand
fix(cli): do not require an agent for `config`
docs: explain the Devide Guard workaround
ci: split macOS into its own workflow
```

Allowed types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `build`, `perf`, `revert`.

## Adding a command to the CLI

1. Add the step to `src/speckfish/prompts.py` (or `SKILL_SPECS`) and `src/speckfish/steps.py`.
2. Register it in `build_parser()` in `cli.py`.
3. If it is a shippable step, add it to `STEPS` in `editors.py` so VS Code and the agents get it too.
4. Tests in `tests/test_smoke.py` must stay hermetic: no network, no agent CLI (use `--dry-run` and
   monkeypatch `get_harness`).
5. Update `README.md` and `CHANGELOG.md`.

## Releasing

Releases are cut from `main` by tag; the workflow builds, verifies and publishes:

```bash
# bump the version in pyproject.toml and src/speckfish/__init__.py, update CHANGELOG.md, merge the PR
git switch main && git pull
git tag v0.1.15 && git push origin v0.1.15
```

`release.yml` then runs the tests, builds the wheel and the sdist, checks that the tag matches the
package version, installs the wheel in a throwaway venv, and attaches the artifacts plus
`SHA256SUMS.txt` to the GitHub release.
