# Changelog

Every release is published with the wheel, the source distribution and `SHA256SUMS.txt` on
<https://github.com/jamartif/speckfish/releases>.

## v0.3.1 — 2026-11-27

- **A suspended computer is no longer read as a silent agent.** On Windows with Modern Standby (also sleep and hibernation) everything is frozen while the lid is closed. On waking, speckfish compared the clock with the agent's last output, concluded "the agent wrote nothing for 600s", killed the step and reported its duration as 248 minutes: a `codex` run of `implement` left running overnight lost its chunk that way. Now a pause longer than 30 seconds (`SPECKFISH_SUSPEND_SECONDS`) between two looks at the agent is taken as the computer being suspended: the pause is not counted as silence or as duration, the silence clock starts over (a healthy agent carries on; one that really stays silent is stopped after the idle limit of *awake* time, with a true reason), and speckfish says so: `the computer was suspended for 4h 08m; that time is not counted`. The step result has `suspended_s`, the summary a `note:` line, and `<step>.meta.json` records it.
- **The duration is taken before the step is closed.** Joining the output readers and clearing the status line can block (a stuck console, a slow disk); that time was added to the agent's.

## v0.3.0 — 2026-11-26

This release adds an interactive console, a permission model and a security and CI audit's worth of fixes. Five
changes need an action when you upgrade (the first block below); everything else is additive.

**Found by the Windows CI before release:** under concurrency `state.json` could lose entries on Windows (38 of 40 saved). Creating the lock file while another process was deleting it fails there with `PermissionError`, which was read as "nothing to protect" and ran unlocked; every writer in a process also shared one temp file name. The lock now waits on that error, temp files are unique per write, and a `state.json` that is busy for a moment is retried and, if it stays busy, raises instead of being read as empty (which would have erased the run's history on the next write).

**Upgrading from 0.2.x**

- Codex needs your consent (`config unsafe=true`, `--unsafe`, or `SPECKFISH_UNSAFE=1`).
- An EGM checkout that comes with a project (`vendor/egm`) is not loaded until `speckfish trust`.
- `@file` only reads inside the project (`--allow-outside-root` overrides it).
- Old run folders are deleted automatically, keeping the newest 50 (`config keep_runs=N`, `0` = never).
- Regenerate the VS Code tasks with `speckfish editors`: they no longer go through a shell.


Full-screen console and README:

- **`speckfish` takes the whole terminal.** The console runs on the terminal's alternate screen (as `vim` or `less` do): earlier commands are out of sight and the terminal is restored exactly on exit — also after an error, `atexit`, SIGTERM or SIGHUP. Output longer than the window goes through a pager (`Enter` for more, `q` to skip), because that screen has no scrollback; `/clear` redraws it. Off with `config fullscreen=false` or `SPECKFISH_FULLSCREEN=0`, and automatically when the terminal cannot take escape codes.
- After the setup wizard the screen is cleared and only its result is kept. Hint lines wrap between words to the terminal width (they used to break `/model` into `/mode` + `l`), the status bar is cut with an ellipsis, and the menu uses plain one-line descriptions instead of the parser's internals.
- README: quick start, eight screenshots of the real console, the console section rewritten, settings and environment variables, exit code 130. The screenshots are regenerated with `python docs/screenshots/capture.py`: real speckfish in a pseudo-terminal, with a scripted stand-in for the agent.

Ctrl+C while an agent runs (reported with Codex on `implement`):

- **It now says what it is doing and whether it worked.** Ctrl+C prints `Stopping the agent…`, kills the whole process tree and *checks* that it is gone. If it is not, it prints `WARNING: the agent (pid N) is still running` with the exact command to stop it by hand (`taskkill /F /T /PID N`); before, a kill that failed was silent and the agent kept writing code with its sandbox off. A second Ctrl+C forces the kill.
- **Windows: one job object per agent** (kill-on-close). A single call ends the whole tree — `node`, the native binary and every shell it started — without depending on `taskkill`, which App Control can block; the OS also does it if speckfish dies. The chain is job → `taskkill /T` → `kill`, and each step is verified.
- **Windows: the console keeps delivering Ctrl+C.** Some agents switch the console input to raw mode, after which Ctrl+C is just a key and never reaches speckfish. While a step runs speckfish puts `ENABLE_PROCESSED_INPUT` back each second and restores the console to how it found it afterwards.
- Anything an agent leaves running after its step finished is stopped with its job (Windows).
- Verified on Linux with a real SIGINT (an agent plus a child that outlives it); the Windows paths are covered with a simulated `kernel32` only, since there is no Windows machine in the test setup.

- `/harness` and `/model` in the console are pickers when given no argument. `/harness` lists the agent CLIs that are installed (codex is marked "no sandbox", and choosing it asks for consent); `/model` lists what the chosen CLI lets speckfish discover: the aliases `claude --help` documents (`fable`, `opus`, `sonnet`), the `choices` Copilot prints in its own `--help`, and the model set in `~/.codex/config.toml`. None of the three CLIs has a command that lists its models, so nothing is invented: pick `default` or type any name the CLI accepts. Both take `--save` to keep the choice in `.speckfish/config.json` (saving codex also saves that you accepted it); without it the choice lasts for the session. The header shows the model when one is set.

Cleaner console output:

- While an agent works there is **one live line** (spinner, step, clock, what it is doing in words, how many actions) cut to the real width of the terminal, so it never wraps. Commands are described instead of dumped: `[System.IO.File]::ReadAllText('C:\\Users\\…\\SKILL.md')` shows as `reading .agents/skills/speckit-constitution/SKILL.md`, `Get-ChildItem -Force .specify; …` as `listing .specify`, `git status --short; …` as `git status`.
- Only **milestones stay on screen**: files written or edited, subagents, warnings. `--verbose` (or `SPECKFISH_VERBOSE=1`, or `/verbose` in the console) keeps every action, in plain words. Without a terminal (CI, a pipe) every action is still one plain line, now without the JSON escaping.
- Each step's summary says what the agent did (`agent: 14 commands · wrote .specify/memory/constitution.md · 2 subagents`) and is coloured (OK green, FAILED/BLOCKER/ERROR red, WARNING yellow). Colour is only used on a terminal; `NO_COLOR` turns it off, and JSON output is never touched.
- The console leaves a blank line between a step's result and the next prompt.
- `workspace` and the tests no longer depend on which test happened to cache `git` first.

Permission model and CI hardening (see `AUDITORIA.md`, Sprints 3 and 4, and `SECURITY.md`).

**Changes you will notice when upgrading**

- **Codex needs your consent.** It runs with its sandbox and approvals off, so `get_harness` refuses it until you say so: `speckfish config unsafe=true`, `--unsafe` for one run, or `SPECKFISH_UNSAFE=1`. `harness=auto` never picks it on its own, and a `--fallback codex` without consent is ignored. The console asks when you choose it.
- **A project's EGM must be trusted.** An EGM checkout inside the project (`vendor/egm`) or named by its `.speckfish/config.json` is only loaded if its content hash is in `~/.speckfish/trusted.json`. `speckfish init` records the pinned checkout it installs; for one you already have, run `speckfish trust` once (it shows what it is trusting). Any changed byte revokes it. `SPECKFISH_TRUST_ALL=1` turns the check off (a disposable CI container).
- **`@file` only reads inside the project.** `--allow-outside-root` overrides it. The file's text now sits between a random `SPECKFISH-INPUT-…` fence and is labelled as data, so it cannot close the block and add instructions.
- Old run folders are deleted automatically, keeping the newest 50 (`speckfish config keep_runs=N`, `0` = never). A run is never deleted while it holds the only review/analyze/drift of a feature or the newest report, so `status` and `report` keep working.

**New**

- Read-only steps (`review`, `drift`, the `--split` inventory) run Claude with `Bash` and `PowerShell` **denied** (`--disallowedTools`); they still write their report. Checked against the real CLI: a denied tool is not loaded at all. Spec Kit steps and `implement` keep their shell. `speckfish config permissions=open` restores the old behaviour for every step.
- Claude receives the prompt on stdin instead of argv (not visible in `ps`, no command-line length limit). The log and `--dry-run` still show it.
- `env_allowlist` / `SPECKFISH_ENV_ALLOWLIST=NAME,PREFIX_*`: the agent inherits only the basics, its provider's variables and what you list.
- `logs/<step>.meta.json` per step: agent and version, argv without the prompt, prompt length and SHA-256, permission profile, EGM commit and trust, cost. No environment variables, no prompt text.
- `/implement` in the console shows what the chosen agent will be allowed to do before asking.
- `SECURITY.md` (threat model, what is and is not protected, how to run in CI), Dependabot for Actions and pip, release build provenance attestations, least-privilege workflow permissions, every Action pinned to a commit SHA, `ruff` with the bandit/perflint/ruff rule sets, `mypy` (strict for `store`, `trust`, `registry`, `workspace`) and a coverage floor of 80 % in CI.

State integrity and speed (see `AUDITORIA.md`, Sprint 2):

- `state.json`, `*.result.json`, `config.json`, `.specify/feature.json`, `specs/features.json` and the batch/`go`/`pipeline` summaries are written atomically (temp file + replace): an interrupted write leaves the previous file, not a truncated one. `state.json` is updated under a lock, so two steps finishing at once no longer lose each other's entry.
- Two speckfish processes started in the same second no longer share a run folder: the id gets `-2`, `-3`, … (claimed with an atomic `mkdir`).
- `status` / `next` / `go` read each `state.json` once (cached until it changes) and build one index of the newest review/analyze/drift per feature instead of re-reading every run for every feature: 20 features over 500 runs went from 1.5 s to 0.4 s.
- `--budget` counts one currency. Claude reports dollars and Copilot credits; a fallback in the other currency used to be added to the cap (30 credits counted as 30 dollars). The cap now applies to the first cost seen, the other currency is reported as `spent_other`, and the stop message says the unit (`budget of 5 usd reached`).
- `specify --review` keeps the review it ran: it is in the result (`review`), shown in the summary, and blockers exit 1 (a failed review exits 2, or 3 on quota/auth). It was discarded before.
- `--feature` with a prefix that matches several features is an error naming them; an exact name or a number wins over a prefix.
- `.specify/feature.json` cannot point outside the project (an absolute path or `../` is ignored).
- `implement` no longer treats `src/bin/main.rs` or any path containing `/bin/` as build output: only `bin/Debug|Release`, `obj/Debug|Release`, `.speckfish/`, `.claude/`, `.agents/`, `.gitignore`, `CLAUDE.md` and `AGENTS.md` are ignored. Its git check also uses the git that `find_git` validated.
- `runs --clean --keep -1` no longer deletes an arbitrary run; the Copilot adapter honours the budget it is given; `plan` sends the stack once instead of twice (a `@stack.md` of 12 KB cost double the tokens). *The `plan` prompt change has not been measured with a real agent.*

Security and robustness (see `AUDITORIA.md`, Sprint 1):

- **EGM is pinned.** `init` used to clone the tip of EGM's `main` and load it as an agent plugin with the agent's full permissions. It now checks out tag `v0.1.1` and verifies its commit (`dea7761…`); if the tag has moved it refuses to use it. `speckfish init --egm-ref <tag|sha>` changes the version on purpose. `doctor` shows the commit and whether it is the pinned one.
- **`.speckfish/` stays out of git.** `init` adds it (and `vendor/egm/` when it cloned EGM) to the project's `.gitignore`; the run logs hold full agent transcripts. `doctor` warns when it is not ignored.
- The VS Code tasks generated by `editors`/`init` use `"type": "process"` instead of `"shell"`: a description with `$(...)`, backticks or `$VAR` was expanded by the shell. Re-run `speckfish editors` to regenerate `.vscode/tasks.json`.
- `specify --split --dry-run` (or an inventory that came back empty) no longer overwrites `specs/features.json` with `{}`, which is what `--only` / `--from-feature` continue from.
- `json_object_with` (recovering the `--split` inventory from a transcript) is linear: a 5 MB log took seconds before and now takes a fraction of a second.
- A corrupt `config.json` is an environment error (exit 3) naming the file, not a traceback. A non-numeric or negative `SPECKFISH_IDLE_SECONDS` / `SPECKFISH_RESULT_GRACE` falls back to the default. An agent binary that cannot be launched is a failed step with a log. A truncated `state.json` no longer stops the next step or `runs`/`show`.
- `SPECKFISH_POLL_SECONDS` (default 1 s, it was a fixed 5 s) is how often the timeout and the inactivity rule are checked; the test suite runs in about half the time.

- `speckfish` with no arguments in a terminal opens an interactive console (`speckfish shell` does the same explicitly). In a pipe or in CI it still prints the usage error, so scripts do not change.
  - First run: a wizard offers to run `init`, lists the installed agent CLIs (claude, copilot, codex) with their version and asks which to use and the document language.
  - `/command` runs the same subcommand the command line would (`/constitution`, `/specify`, `/plan`, `/review`, `/go`, `/status`, …); a bare line is offered as `/go <idea>`; an empty line runs the suggested next step after asking.
  - Steps that take free text (`/constitution`, `/specify`, `/plan`, `/go`, `/brainstorm`) open a multi-line editor when no text is given. `@file.md` imports a file.
  - `/implement` and `/go --implement` ask for confirmation instead of needing `--yes`; selecting codex warns that it runs without a sandbox. `/harness` and `/model` change the agent for the session only.
  - Ctrl+C stops the running step and returns to the prompt.
  - Optional extra `pip install "speckfish[tui]"` (prompt_toolkit): a `/` menu that opens as you type with each command's help, `@file.md` path completion limited to the project, history in `.speckfish/history`, a status toolbar with the suggested next step, a multi-line editor and arrow-key pickers. Without it the console works with plain `input()`.
  - Free-text steps show a preview before sending (Send / edit / cancel). `/feature` picks the active feature (arrows, a number, or `/feature 002`).
  - The command list, `/help` and the menu are read from the CLI's own parser, so a new subcommand shows up with no second list to maintain.
  - The wizard also asks for a fallback agent and stores it as `fallback` in `.speckfish/config.json` (`speckfish config fallback=copilot`); every command uses it as `--fallback` when none is given.

## v0.2.1 — 2026-11-24

- An agent that printed its final event (`result` for Claude/Copilot, `turn.completed` for Codex) but does not exit is closed after 45 s (`SPECKFISH_RESULT_GRACE`), instead of waiting 10 minutes for the idle limit; what the step wrote decides the verdict. Seen with Claude on Windows after an EGM review.
- Ctrl+C stops the agent's whole process tree (it used to leave `codex exec` running with the sandbox off) and exits with 130 and a one-line message instead of a traceback.
- `revise` timeout default is 3600 s (it was 1800 and `--majors` needs more). README: `config timeout.revise=3600`, not `--set`.

- `revise --report FILE` fixes the feature in one pass from a review report that already exists (a review run by hand, or one recorded as failed although its reports are complete), without paying for another review.

- `go` splits a big brief given as `@file`: the size check measured the 158-character "read this file" instruction instead of the file, so a 22 KB brief was never split.

- `go`, `next` and `revise` read the blocker and major counts again from the consolidated report instead of trusting the number saved in `state.json`, so a review recorded as "0 blockers" by an older parser is corrected. `revise --majors` also revises when only Majors are left.

- The consolidated review report is read when each finding has its own heading (`### B1. ...`, as the Codex review writes it): `go` no longer says "no blockers" on a report with 4 blockers and 25 majors. Severity headings match whole words only (a heading with "monitoring" or "limit" no longer switches to Nits) and a bare `## Verdict` heading takes the sentence under it.

- Review fixes: `go --implement` no longer stops mid-feature (the step cap counts only design steps); `count_tasks` and `parse_tasks` read the same lines (indented subtasks count, code blocks do not) and tasks without a T-number are named by their text; `reconcile` is rejected if it edits anything but checkboxes and records which tasks it ticked; the task total is read after the step; `--chunk`/`--max-chunks` refuse negative values; `latest_record` matches the feature name exactly; the fallback harness sticks for the rest of a loop and also covers `converge`.

## 0.2.0

- `implement` works in chunks (`--chunk N`, default 5; `--max-chunks`, `--budget`). One agent call over
  a whole `tasks.md` (92-173 tasks on a real project) ended after "fixing the critical points", wrote part
  of the code and ticked nothing. Now each call gets the next N pending tasks by id, must tick each as it
  finishes, and a cheap `reconcile` pass ticks the ones whose code exists but were left unticked.
  The loop stops when every task is ticked, after two chunks that tick nothing, or on budget.
  `go --implement` and `next` use the same chunks. `--chunk 0` keeps the old one-call behaviour.

- Phase 1 of `PLAN.md`, fewer steps:
  - `speckfish go "<idea|@brief>"`: init, specify (split automatically for a long brief), plan, tasks,
    analyze, review, and fix-and-review until no blockers; `--implement --yes` also implements, checks
    drift and converges once. Resumable: it derives where it is from `specs/` and the recorded steps.
  - `speckfish revise`: review -> agent fixes the blockers -> review again, up to `--max-rounds`.
  - `speckfish next` (`--explain` to only say what it is) and `speckfish status` (phase, tasks,
    review, next command and total cost per feature).
  - `--budget` caps what one `go` may spend (usd or credits); `--fallback` applies to every step.
  - A review is "current" only until spec.md/plan.md change; ticking tasks does not invalidate it.

- Phase 2 of `PLAN.md`, structure (no change to the commands):
  - `run_step` takes the **kind** of step (`review`) apart from its **label** (`review-001-foo`):
    budgets, timeouts and validation follow the kind; folders, logs and state follow the label.
    A batch review used to lose its budget and a `plan` batch shared one `state.json` entry.
  - New `registry.py`: one `StepSpec` per kind (requires, produces, timeout key, schema, validator).
    `postprocess` is now a dispatch to the registered validator instead of an if-chain.
  - New `workspace.py` (task progress, git snapshot, current feature).
  - One `run_batch` replaces three near-identical batch loops (fallback, skip, stop, totals).
  - `cli.main` is a table of handlers (`HANDLERS`), one function per command.
  - `Harness.slash_commands` replaces the `name == "claude"` checks.
  - Fixed: plain `speckfish review` (no flags) fell through every branch and exited 2 silently
    since #28.
  - `constitution` had no default timeout (fell back to 600 s); it now has 1200 s.

- Phase 0 of `PLAN.md`, batch correctness:
  - `review/drift --all-features` and `specify --split` now validate each step as its kind
    (`review-001-x` was never recognised), so blockers set exit 1 and a drift with no report fails.
  - Drift reports, logs and `state.json` entries are per feature (they overwrote each other).
  - `specify --split --review` keeps one review per spec.
  - `specify`/`plan`/`--split` with copilot or codex expand `@file` and apply `language`.
  - An agent closed for inactivity is judged by what it wrote (`spec.md`, `plan.md`, `tasks.md`,
    reports), not only by reports.
  - `implement` compares the working tree before and after: an already-dirty repo no longer passes.
  - `review`/`drift` default to the same feature as `plan`/`tasks` (`.specify/feature.json`).
  - `--with-clarify`/`--with-analyze` refuse batch flags instead of ignoring them;
    `review --with-analyze` with no target analyzes the current feature.
  - Quota/auth detection needs HTTP or billing context (a bare `429` or `credit` no longer matches).
  - Spec Kit skills for copilot/codex are read from `.github/skills` / `.agents/skills`.
  - Windows fallback path for `git.exe` was misspelled.
  - Tests run against `src/` (`pythonpath`), lint runs in CI, `pip install -e ".[dev]"`.

- The `implement` prompt now asks the agent to tick each `- [ ]` as it finishes. Without that the work
  was on disk but `tasks.md` stayed untouched: 10 tasks done, 0 recorded, and the next run would start
  from T001 again.


- `implement` now reports how much of `tasks.md` it actually did (`OK implement · tasks 16/113`) and
  fails when it ticks **no** task at all. Four implements this night reported OK, cost 51.5 credits
  and left 871 tasks pending and three endpoints in the whole API: "something changed in the repo"
  was too weak a criterion for a 113-task feature.


- The "did it write code?" check ignored `bin/`, `obj/`, `.speckfish/` and tool state: a `dotnet build`
  alone touched `obj/*.AssemblyInfo.cs` and counted as an implementation.


- Fix: `implement` reported **OK** after the agent only ran the skill's preparation phase — 2 minutes,
  1.8 credits and not a single file written. The step now fails when the repository is unchanged
  ("the agent answered but wrote no code").
- Fix: for copilot/codex the prompt never named the feature, so the agent followed
  `.specify/feature.json` and worked on the last one instead of `--feature 001`. It now says
  `Work ONLY on the feature in <dir>`.
- `implement` gained a headless note: write the files from tasks.md and run the build, do not stop at
  a preparation/validation report.


- An agent that finishes its work and never exits (EGM's last pass does this) no longer burns the whole
  timeout: a process that writes nothing for `SPECKFISH_IDLE_SECONDS` (default 600) is stopped, and the
  step counts as done when the artifacts are on disk. A review sat in `finishing` for 31 minutes with
  its report already written and still ended up FAILED.


- A batch stopped by a quota/auth failure now exits **3** (environment) instead of 2, so CI and the
  operator can tell "out of credits" from "the step failed".
- `total_cost` in the batch summary sums credits as well as dollars (Copilot bills in credits, so it
  always read 0.0).


- Fix: a `--dry-run` batch wrote `batch-review.json` with every step `ok: true`, and `review
  --skip-existing` then treated those features as reviewed — it skipped five real reviews without
  spending anything. Dry runs never count as done now.


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
