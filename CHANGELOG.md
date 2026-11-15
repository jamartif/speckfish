# Changelog

Every release is published with the wheel, the source distribution and `SHA256SUMS.txt` on
<https://github.com/jamartif/speckfish/releases>.

## Unreleased

- `--fallback <harness>`: when a step stops for quota/auth, the same feature is retried once with the
  other agent CLI, so an overnight batch finishes instead of dying at 40% of a Copilot allowance.
  Only quota/auth switches — anything else would fail on both. Every switch is listed in the summary.


- `review --skip-existing` / `drift --skip-existing` now work across runs: a feature whose review
  already succeeded in an earlier run is skipped, so resuming after a quota stop does not pay for the
  same review twice. `review`'s flag was accepted but hard-wired to false.


- Fix: a batch runs each step as `<step>-<feature>`, and that label was used to look up the timeout —
  so `review` (1800 s) and `tasks` (1800 s) fell back to the generic 600 s. `tasks` survived by 8 s;
  `review` was killed at 10 minutes with its work half done. The timeout now resolves to the base
  step, and the hint names the key that actually exists (`timeout.review`, not `timeout.review-001-…`).
- `review` default raised to 2400 s: a Copilot review with ten subagents runs past 10 minutes.


- Fix: `clarify` (and `checklist`, `converge`, `brainstorm`, `draft`) had **no default timeout**, so
  `run_step` fell back to the generic 600s and a real 7-feature run lost feature 5 after ten minutes
  of work — the agent had already written its `questions.md`. Every agent step now has its own
  default (`clarify` 1200s, like `analyze`), and a project config that only stores a few keys still
  gets the new default for the rest.


- Fix: a batch also shared one **log** per step (`logs/clarify.log`), overwritten on every feature, so
  there was nothing to watch while it ran. Logs are now `logs/<step>-<feature>.log`, same as artifacts.


- Fix: a batch (`--all-features`) ran every feature into the **same** artifact folder, so
  `clarify --all-features` on 7 features left a single `questions.md` and the last feature overwrote
  the other six (and each agent could read the previous feature's file). Every feature now writes to
  `artifacts/<step>-<feature>/`.


- Fix: `--skip-existing` used `tasks.md` as the marker for `analyze`, so right after `tasks` every
  feature counted as "already analyzed" and nothing ran. Only `plan`, `tasks` and `checklist` own a
  file; `analyze`, `clarify` and `converge` never skip.


- Uniform resume flags on **every** feature-oriented step (`plan`, `tasks`, `analyze`, `clarify`,
  `checklist`, `converge`, `review`, `drift`): `--feature`, `--only 2,4,5`, `--from-feature 4`,
  `--all-features`, `--skip-existing`. One `select_features()` decides what runs, so the flags mean the
  same thing in every command (`specify --only` already did this over the saved cut).


- `review --all-features` and `drift --all-features`: walk every feature instead of asking the user to
  write a PowerShell/Bash loop. Review takes `plan.md` when it exists and `spec.md` otherwise, uses a
  per-feature artifact directory and writes `batch-review.json` / `batch-drift.json`.


- `--skip-existing` for `plan`/`tasks`/`analyze` with `--all-features`: skip the features whose
  artifact is already there and say which ones were skipped, so re-running an interrupted batch does
  not redo (or re-pay) finished work.


- `plan --with-clarify [--accept-defaults]`: runs `clarify` first and **stops** if it left questions
  (planning on unreviewed assumed defaults is how a misunderstanding gets into the design).
- `review --with-analyze`: runs the cheap Spec Kit `analyze` before the paid EGM review.
- Diagram renumbered 1→11 with `clarify` as its own step between `specify` and `plan`.


- `clarify` reports what it actually did: *"5 clarification question(s), each with the default it
  assumed — the spec was NOT modified"*, with `questions.md` marked as the file to open. Before, the
  step just said OK and the questions sat unnoticed in the run directory. It now fails loudly if the
  agent finished without writing `questions.md`.
- README: the order table includes `clarify` (before `plan`), and the review example no longer mixes
  `--interactive` with a positional argument that was read as the document.


- Fix (wasted run time): the codex hint now explains the mojibake trap. This machine's PowerShell 5.1
  reports output encoding **ibm850**, so `Get-Content` shows `CatÃ¡logo` for a file that is correct
  UTF-8 on disk. A 15-minute `plan` spent ~4 of them grepping for mojibake that never existed. The
  prompt now says: read with `-Encoding utf8` or `[System.IO.File]::ReadAllText(..., UTF8)`, write with
  `[System.IO.File]::WriteAllText(..., UTF8Encoding::new($false))`, and never rewrite a file to "fix"
  what only the console is mangling.


- A `--all-features` run now writes `batch.json` in the run directory. `state.json` is keyed by step
  name, so seven features overwrote each other and only the last one survived.
- Codex prompt: no `pip install` (a run installed `openapi-spec-validator` on the machine to validate a
  contract) and read whole files with `Get-Content -Raw` instead of line ranges — eight commands where
  one would do.


- Fix: a `.speckfish/config.json` written by an older build stored **all** the defaults, so it pinned
  `timeouts.plan = 600` forever and a plan that was writing files at minute 9 was killed at 10. Configs
  now carry `schema: 2`; anything older drops the stored `timeouts` block once and picks up the current
  defaults. And `save()` no longer writes a value that equals the default.
- Codex prompt: it is now told this session has **no `apply_patch`** and how to write files instead. In
  the run that hit the timeout, 49 `*** Begin Patch` blocks and 25 `apply_patch` lookups ate ~6 of the
  10 minutes, with broken escapes (`
+#`) in the retries.


- README: the `Development` section is now just "how to run the tests" plus a pointer; the release
  procedure (tags, workflow) moved to `CONTRIBUTING.md`, where it belongs, and states the current
  policy: no release per change, install editable until one is asked for.

- README status table: Codex is no longer "unverified adapter". It is verified for the Spec Kit steps
  against an OpenAI-compatible provider (nan.builders / deepseek-v4-flash) and explicitly **not**
  verified for the EGM review, which loops on `collab: SpawnAgent / Wait` with that model.
- README explains harness vs provider: `--harness codex` + nan is still the codex harness; nan only
  supplies the model.
- The Claude harness note in `doctor` was still in Spanish; now English.

- Diagram fixed and renumbered 1→9: `drift` is now **after** `implement` (it compares documents with
  code that exists), `tasks` is explicitly before `review`, and the optional `brainstorm`/`draft` arrow
  points at `specify` instead of `constitution`. The README states the order in a table with the reason
  for each position.

- README: full **command reference** (all 22 commands with their real flags, global flags included) and
  a **worked example** end to end with measured timings.
- `docs/flow.svg` redrawn: four lanes (discover / specify / verify / ship) with `brainstorm`, `draft`,
  `report`, `--split/--only/--from-feature`, `--all-features`, and `codex` marked verified against an
  OpenAI-compatible provider. Mermaid diagrams updated to match (tasks + report in the sequence).

- New `speckfish brainstorm <idea|@file>` and `speckfish draft <concept|@file>`: EGM's exploration
  skills (concepts_brief.md / design_draft.md) in their own artifact directory, for when the input is
  still a vague idea. The step fails if the expected artifact is missing, and the prompt marks every
  unconfirmed decision as a hypothesis. They are conversational by design — expect drafts, not
  decisions.

- Fix (hang): `activity()` took the status lock and then called `_clear()`/`_write()`, which re-took
  the same non-reentrant `threading.Lock`. The thread reading the agent's output blocked on itself, so
  the clock froze on the same second and nothing else was ever read. Now an `RLock`, with a regression
  test that fails with a plain `Lock`.
- Project policy change: **no release per change**. Commits go to `main` and the CLI is installed
  editable from the working copy (`pip install -e`) until a release is explicitly approved.

## 0.1.29

- Fix: `--split --dry-run` executed the agent for real and wrote specs (`dry_run` was not forwarded to
  the per-feature step). `--dry-run` now reports what it would specify and writes nothing.
- `specify --split --only 5,6,7` / `--from-feature 5`: continue an interrupted split from
  `specs/features.json` without paying for the inventory again. `description` is optional with them.
- A per-step timeout now says so and how to fix it: *"timed out after 600s — the agent may simply need
  longer. Raise it with `speckfish config timeout.specify=1200`"*.
- Timeouts are configurable (`speckfish config timeout.<step>=N`) and the defaults are generous
  (specify/plan/tasks/review 30 min, implement 60 min): a slow local model takes 6-9 min per spec and
  was hitting the old 10-minute wall mid-run.

## 0.1.28

- The progress line now shows **what the agent is doing**, not just a clock: one permanent line per
  command run (`$ Get-Content -Raw …/SKILL.md`), file written (`wrote specs/001-…/spec.md`) and
  subagent spawned.
- `codex` runs with `exec --json`: its NDJSON events (item.started/completed, turn.completed) are
  consumed live, which is what feeds the activity lines, plus its last message and token usage for the
  step result. Verified against a real run: events arrive at 0.4 s / 4.9 s / 5.0 s of a 10 s run.
- Shell wrappers are stripped from the display (`"C:\…\powershell.exe" -NoProfile -Command "…"` →
  the command itself), paths are shown relative to the project, and repeats are collapsed.

## 0.1.27

- Fix: `--split` listed the features and then aborted with "the agent did not return a usable feature
  list" when the harness has no `--json-schema` (codex): the loop still used the old
  `structured`-only lookup. It now uses the recovered inventory, so N features produce N specs.
- The console no longer echoes the agent's JSON, even on harnesses that echo prose: whole JSON lines
  and lines over 1200 characters stay in the log (`is_noise_line`).
- `specify --split` ends with a short result — how many specs, their paths, the run duration — and for
  `--inventory-only`, the proposed count plus where the cut was saved.

## 0.1.26

- Fix: the progress line was disabled for harnesses that echo prose (`codex`), which is exactly the
  case where its own output says nothing about how long it has been running. It now always shows, as
  standalone lines every 10 s when the agent owns the terminal (`plain_only`), so the clock and stage
  never overwrite the agent's text.

## 0.1.25

- `plan --all-features`, `tasks --all-features`, `analyze --all-features`: run the step over every
  feature under `specs/` in creation order, one shared run id, stopping at the first failure with a
  per-feature summary (`OK`/`FAILED`, duration, cost) and the point where it stopped. `implement` is
  excluded on purpose.

## 0.1.24

- `specify --split --inventory-only`: stop after the inventory, write `specs/features.json` and print
  the proposed cut, so the feature split is approved before paying for one spec per feature.
- The split inventory is recovered from the step log when the harness has no `--json-schema` (codex,
  copilot). `json_object_with()` picks the richest `features` array and ignores JSON schemas
  (`properties`) and one-item examples, which a real codex log contains.

## 0.1.23

- Codex adapter verified against a real OpenAI-compatible provider (`nan.builders`,
  `deepseek-v4-flash`): `specify` produces a full `spec.md`.
  - Runs `--dangerously-bypass-approvals-and-sandbox`: codex-cli 0.169 keeps `exec` in a read-only
    sandbox even with `--sandbox workspace-write` or `-c sandbox_mode=workspace-write`, so the agent
    answered in prose and wrote nothing. The notes say so in `doctor`.
  - `wire_api = "chat"` is no longer accepted by codex; the provider must expose `/v1/responses`.
- `node_entry()` also finds npm packages that ship `bin/*.js` (codex), so the Windows shim prefix
  resolves and `doctor` no longer crashes with WinError 193.
- `run_cmd()` catches `OSError`, and `doctor()` reports a broken binary instead of raising a traceback.
- A `specify` step that writes no `specs/*/spec.md` is now a failure, not a success with a nice
  essay in the log.

## 0.1.22

- Live progress while an agent works: `[review] 01:42 | subagents 5 | tools 31 | last
  egm:egm-zoom-out`, refreshed in place once a second (one line every 15 s when piped). Built from the
  events the CLI already emits, so it costs nothing. Erased when the step ends. `--no-progress` turns
  it off (`--quiet` / `--json` imply it).
- Claude now runs with `--output-format stream-json --verbose` instead of `json`: that is what makes
  the progress possible. Output parsing reads the last `result` event, with the previous whole-object
  parsing kept as a fallback.
- Quota failures also report when the window reopens (`weekly window reopens at 20:00`), read from
  `rate_limit_info.resetsAt`, and recognise the `rate_limit_event` / `error: rate_limit` shapes.
- New fixture `tests/fixtures/claude_quota_stream.jsonl`: a real captured stream-json run that hit the
  weekly limit, used by two tests.

## 0.1.21

- Failed runs no longer dump the agent's JSON: `human_error()` classifies the failure
  (`quota` / `auth` / `agent` / `unknown`) and reads `api_error`, `api_error_status` and `result`,
  producing e.g. *"the agent ran out of quota (HTTP 429): You've hit your weekly limit · resets 8pm"*.
  Plain-text failures keep only the last two useful lines.
- Out-of-quota and bad credentials now exit with **3** (environment) instead of 1, and the summary adds
  a `fix:` line suggesting to wait or switch `--harness`.

## 0.1.20

- Console output is readable: the agent's raw JSON is no longer echoed at the user (it is still in
  the log). JSON-output harnesses (`claude`, `copilot`) set `echo_stdout = False`; only prose output
  (`codex`) is streamed.
- New summary layout: verdict first, one line of counts, human durations (`3m22s`), a rounded cost
  (`$1.29`), and report paths relative to the project with the consolidated one marked
  `<- start here`.
- New `speckfish report`: prints the consolidated review/drift report of a run in the terminal
  (`--full` for everything).
- `state.json` now keeps `majors`, `nits`, `meets_standard`, `verdict`, `denials`, `artifact_dir`
  and `log`, so `speckfish show` / `runs` do not lose what the step found.

## 0.1.19

- New `language` setting (default `en`): every prompt ends with *"Write every document, report and
  answer in English, regardless of the language of the input material"*, so specs, plans and reports
  no longer follow the language of the brief. `speckfish config language=es` switches a project back
  to Spanish.
- Test data and fixtures no longer carry Spanish text (except the EGM report fixture, which is a real
  Spanish output kept on purpose to prove the parser handles both languages).

## 0.1.18

- `speckfish features`: lists every feature under `specs/`, which artifacts each one has (spec/plan/
  tasks) and which one is active.
- `--feature <002|name|path>` on `plan` and on every skill step (`tasks`, `analyze`, `checklist`,
  `clarify`, `converge`, `implement`): switches the active feature (writes `.specify/feature.json`)
  and names it in the prompt, so `plan` no longer silently plans the last spec created.
- `--root ~/repo` and `--root /c/repo` now resolve on Windows (MSYS-style paths).

## 0.1.17

- `specify --review` (also with `--split`): runs the EGM goldfish review on each spec right after it
  is written. Without it, `specify` never calls EGM; `review` stays a separate step.
- Windows fix: `PowerShell` is now an allowed tool for Claude. A real run had
  `.specify/scripts/powershell/resolve-template.ps1` **denied**, so Spec Kit worked around its own
  template resolution.
- Permission denials are reported in the step summary instead of being silently dropped.

## 0.1.16

- `speckfish specify --split [--max-features N]`: inventories a big brief as JSON and runs one
  `/speckit-specify` per independent feature. Spec Kit itself creates exactly one feature per
  invocation, so a multi-idea brief used to end up in a single mixed spec (verified: a 3-request
  brief produced one spec containing all three).

## 0.1.15

- Workflow `pr.yml`: validates the PR title (Conventional Commits), labels by type and size and
  requests reviewers (`.github/CODEOWNERS` + optional `REVIEWERS` repository variable).
- `lint` job (`ruff check src tests`) on every PR; ruff is configured in `pyproject.toml` and the
  existing findings are fixed (no rule silenced except `E501`).
- `CONTRIBUTING.md`: every change goes through a pull request; `main` is protected.
- `pr-review.yml`: generated, self-updating review checklist comment on each PR (areas touched, what
  to verify, reminders for harness/prompt/version changes).
- Branch protection on `main`: pull request required, six required checks, admins included.

## 0.1.14

- `editors` deletes the commands generated by older versions (`speckfish-*`) so they do not show up
  twice in the slash menu next to the new `spfish-*` ones. Files it did not generate are not touched.

## 0.1.13

- Editor commands use the short `spfish-` prefix by default (`/spfish-review`) so they no longer blur
  into Spec Kit's own `/speckit-*` commands in the slash menu; change it with `--prefix`.
- `speckfish init` now generates the VS Code tasks and the agent slash commands by default
  (`--no-editors` to skip).

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
