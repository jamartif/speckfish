# Security

## Reporting a vulnerability

Please report it privately through GitHub: **Security → Report a vulnerability** on
<https://github.com/jamartif/speckfish/security/advisories/new>. Do not open a public issue for it.
Include the version (`speckfish --version`), the agent CLI you use and the smallest input that reproduces it.

## What speckfish is, from a security point of view

speckfish launches **agent CLIs** (Claude Code, Copilot CLI, Codex) as subprocesses, hands them text, and
reads back files. The agent is what can do harm: it reads your repository and anything you give it, and it can
run commands and write files with *your* permissions. speckfish's job is to give it as little as the step needs
and to avoid feeding it things you did not choose. It is not a sandbox.

**Assume that any text the agent reads can contain instructions** (a pasted issue, a README of a dependency,
a spec written by someone else). That is prompt injection, and no tool can fully prevent it; the defences below
limit what an injected instruction can do.

## What it does

| Risk | Defence | Where |
|---|---|---|
| A read-only step (review, drift, the `--split` inventory) runs commands | The Claude adapter **denies** `Bash` and `PowerShell` for those steps (`--disallowedTools`). Checked against the real CLI: a denied tool is not even loaded for the agent, and it still writes its report. `config permissions=open` restores the old behaviour | `harnesses.py`, `registry.py` |
| An agent with no sandbox | `codex` runs with sandbox and approvals off; it is **refused** until you accept it: `config unsafe=true`, `--unsafe` for one run, or `SPECKFISH_UNSAFE=1` | `harnesses.get_harness` |
| A repository ships its own EGM plugin (`vendor/egm`, or `egm_root` in `.speckfish/config.json`) | EGM is loaded as a plugin with the agent's permissions, so it must be **trusted**: its content hash has to be in `~/.speckfish/trusted.json`. `init` records the pinned checkout it verified; `speckfish trust` records one you read. Any changed byte revokes it | `trust.py` |
| EGM upstream changes under you | `init` checks out a pinned tag and **verifies its commit**; a moved tag is refused. `init --egm-ref` changes it on purpose | `steps.install_egm` |
| Secrets in the environment reach the agent | Opt-in allowlist (`config env_allowlist=NAME,PREFIX_*` or `SPECKFISH_ENV_ALLOWLIST`): the agent then gets only the basics, its own provider's variables and what you list | `config.agent_env` |
| `@file` sends a file you did not mean to | `@file` only reads inside the project (symlinks resolved); `--allow-outside-root` overrides it. The content is fenced with a random delimiter and labelled as data | `prompts.read_input` |
| The prompt is visible to other users (`ps`) | Claude takes the prompt on **stdin**, not in argv | `harnesses.ClaudeHarness` |
| Run logs hold full transcripts and end up in git | `init` adds `.speckfish/` (and `vendor/egm/`) to `.gitignore`; `doctor` warns when it is not ignored | `steps.ensure_gitignore` |
| A pasted description runs in a shell (VS Code tasks) | The generated tasks use `"type": "process"`: each argument is passed untouched | `editors.tasks_json` |
| Supply chain of the project itself | GitHub Actions pinned to commit SHAs, least-privilege `permissions`, Dependabot, and build provenance attestations on releases | `.github/` |

## What it does not do

- **`implement` and the Spec Kit steps (`specify`, `plan`, `tasks`, `analyze`, …) keep a shell.** Spec Kit's
  skills run their own scripts through it, and `implement` has to build and test your code. A prompt-injected
  agent in those steps can run commands as you.
- **Copilot and Codex are not scoped per step.** Copilot runs with `--allow-all-tools --allow-all-paths`.
- **The allow-list is not a restriction.** Verified with the real Claude CLI: `--allowedTools` only
  pre-approves; what actually removes a tool is a deny rule. Do not rely on narrowing `Bash(...)` patterns.
- It does not inspect what the agent writes. Review the diff of `implement` like any other change.

## Running it in CI or on code you do not trust

1. Use a **disposable container or VM** with no credentials other than the agent's own API key.
2. Do not give the job a `GITHUB_TOKEN` with write access, deploy keys or cloud credentials. If an agent step
   needs the token, scope it to that job.
3. Set `SPECKFISH_ENV_ALLOWLIST` so the agent inherits only what it needs, and `SPECKFISH_UNSAFE=1` only if
   you really use Codex.
4. `speckfish trust` in the job *after* you have pinned or reviewed the EGM you want, or let `speckfish init`
   install the pinned one. `SPECKFISH_TRUST_ALL=1` switches the check off and is meant for that same disposable
   container.
5. Never run `speckfish` with `--allow-outside-root` on input you did not write.
6. Keep `permissions=scoped` (the default).

## Supported versions

Only the latest release receives fixes.
