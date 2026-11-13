# Changelog

Every release is published with the wheel, the source distribution and `SHA256SUMS.txt` on
<https://github.com/jamartif/speckfish/releases>.

## 0.1.12

- Fix: `config`, `runs`, `show` and `--version` no longer fail when no agent CLI is installed (the
  harness is resolved lazily, only by the commands that actually run an agent).

## 0.1.11

- `speckfish --version`.
- `speckfish config` — show or store project defaults (`harness`, `model`, `profile`, `egm_root`,
  `specify`) in `.speckfish/config.json`; unknown keys are rejected.
- `speckfish runs` now shows per-run steps, cost and duration; `speckfish runs --clean --keep N`
  prunes old runs. `speckfish show` prints a per-step table plus the run total.
- `speckfish pipeline --with-tasks --with-analyze` chains `specify → plan → tasks → analyze → review`
  before the review, still honouring `--gate`.
- Run totals (`cost`, `duration_s`, `cost_note` when units differ) are written into
  `pipeline.json` and computed on demand for `show`.

## 0.1.10

- Any text argument accepts a Markdown file: `@path.md` or a path that clearly is one
  (`.md/.markdown/.txt/.rst`). Files under 12 KB are inlined into the prompt, bigger ones are
  referenced by absolute path. Missing file = clear error, not a silent fallback.
- Verified end to end: `speckfish constitution @constitution-template.md` produced a Spec Kit
  `constitution.md` carrying the five principles of the template.

## 0.1.9

- Every remaining Spec Kit skill is a subcommand: `constitution`, `tasks`, `analyze`, `checklist`,
  `clarify`, `converge`, `implement`.
- `implement` is gated: it refuses to run without `--yes` (it writes code and tests).
- `clarify` in headless runs writes its questions to `artifacts/clarify/questions.md` instead of
  asking into the void.

## 0.1.8

- Fix: the pipeline test no longer needs an installed agent CLI (CI has none).

## 0.1.7

- `speckfish editors`: VS Code tasks, Copilot prompt files, Claude slash commands and Codex prompts.
- Report which model each step actually used (`--model` to pin it).

## 0.1.6

- `pipeline` shows drift in `--dry-run` and skips it when `--gate` already stopped the chain.

## 0.1.5 and earlier

- 0.1.5 `--model` forwarded to every harness; model recorded per step.
- 0.1.4 `init --integration all|list`, `--ignore-agent-tools` when a CLI is missing.
- 0.1.3 `doctor` prints a readable summary (no ASCII banner, no JSON dumps).
- 0.1.2 `python -m speckfish` entry point and `docs/speckfish.bat` for machines where Device Guard
  blocks the generated `.exe`.
- 0.1.1 `review --interactive` (EGM asks, the operator answers), README diagrams.
- 0.1.0 first release: specify/plan/review/drift, per-run artifacts, exit codes, Claude + Copilot.
