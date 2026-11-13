"""Prompt templates, review criteria profiles and structured output schemas."""
from __future__ import annotations

from pathlib import Path

from .config import norm

NO_QUESTIONS = ("Headless run with no user: nobody can answer questions. "
                "Do not ask for confirmation, assume reasonable defaults and finish the task.")

INTERACTIVE = ("Interactive session with a human operator: ask every open question you have "
               "(presets, scope, ambiguities) and wait for the answer before continuing. "
               "The operator can answer in this terminal.")

# --- criteria profile: removes format noise when reviewing Spec Kit artifacts ---
PROFILES = {
    "speckit": (
        "REVIEW CRITERIA SPECIFIC TO SPEC KIT (apply on top of the standard): "
        "this document is a GitHub Spec Kit artifact, not a standalone feature spec. "
        "Do not report as Blocker or Major the absence of: 'Alternatives Considered', "
        "'Risks & Mitigations', 'Problem Statement', 'Goals & Non-Goals', 'Success Criteria'. "
        "Those sections live in spec.md or are out of scope for the reviewed document, so their "
        "absence here is correct by template design. Instead evaluate: consistency with spec.md, "
        "justified technical decisions, explained mechanisms (atomicity, configuration, "
        "exit codes, errors), test strategy and concrete technical risks of the chosen design."
        ""
    ),
    "none": "",
}


def specify_prompt(idea: str) -> str:
    return f"/speckit-specify {idea.strip()}\n\n{NO_QUESTIONS}"


def plan_prompt(stack: str) -> str:
    stack = (stack or "").strip()
    extra = f"Stack and constraints: {stack}\n\n" if stack else ""
    return f"/speckit-plan {stack}\n\n{extra}{NO_QUESTIONS}"


def review_prompt(target: str, artifact_dir: Path, profile: str = "speckit",
                  stage: str = "uninterrupted", interactive: bool = False) -> str:
    if interactive:
        head = [
            f"/egm:egm-review {norm(target)}",
            INTERACTIVE + " Do not pass Step: let me choose the review mode in your menu.",
            f"EGM artifact directory: {norm(artifact_dir)}",
        ]
    else:
        head = [
            f"/egm:egm-review {norm(target)} Step={stage}.",
            NO_QUESTIONS + " No extra Goldfish context (empty list).",
            f"EGM artifact directory: {norm(artifact_dir)}",
        ]
    p = PROFILES.get(profile, "")
    if p:
        head.append(p)
    return "\n\n".join(head)


def drift_prompt(target: str, artifact_dir: Path) -> str:
    return "\n\n".join([
        f"/egm:egm-drift-report {norm(target)}.",
        NO_QUESTIONS,
        f"EGM artifact directory: {norm(artifact_dir)}",
    ])


# --- prompts for harnesses without slash commands (copilot/codex) ---

def egm_prompt_for(harness: str, kind: str, target: str, artifact_dir: Path,
                   egm_root: Path, profile: str = "speckit", stage: str = "uninterrupted",
                   interactive: bool = False) -> str:
    """Copilot/Codex do not resolve `/egm:...`: the prompt asks them to read the package SKILL.md."""
    docs = {"copilot": "docs/copilot.md", "codex": "docs/codex.md"}.get(harness, "docs/claude.md")
    skill = {"review": "egm-review", "drift": "egm-drift-report",
             "brainstorm": "egm-brainstorm", "draft": "egm-draft"}[kind]
    doc = f"with Step={stage}." if (kind == "review" and not interactive) else "."
    head = [f"EGM package root: {norm(egm_root)}",
            f"Read that package's {docs} and run {skill} on {norm(target)} {doc}"]
    if kind == "review":
        if interactive:
            head.append(INTERACTIVE + " Do not pass Step: let me choose the review mode in your menu.")
        else:
            head.append(NO_QUESTIONS + " No extra Goldfish context (empty list).")
    else:
        head.append(INTERACTIVE if interactive else NO_QUESTIONS)
    head.append(f"EGM artifact directory: {norm(artifact_dir)}")
    p = PROFILES.get(profile, "")
    if p and kind == "review":
        head.append(p)
    return "\n\n".join(head)


# --- structured output schemas (claude --json-schema) ---

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["verdict", "reports"],
    "properties": {
        "verdict": {"type": "string"},
        "meets_standard": {"type": "boolean"},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "majors": {"type": "integer"},
        "nits": {"type": "integer"},
        "reports": {"type": "array", "items": {"type": "string"}},
    },
}

DRIFT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "reports"],
    "properties": {
        "verdict": {"type": "string"},
        "drift_found": {"type": "boolean"},
        "reports": {"type": "array", "items": {"type": "string"}},
        "top_items": {"type": "array", "items": {"type": "string"}},
    },
}

SPECIFY_SCHEMA = {
    "type": "object",
    "required": ["summary"],
    "properties": {
        "summary": {"type": "string"},
        "spec_path": {"type": "string"},
        "feature_dir": {"type": "string"},
    },
}

PLAN_SCHEMA = {
    "type": "object",
    "required": ["summary"],
    "properties": {
        "summary": {"type": "string"},
        "plan_path": {"type": "string"},
        "artifacts": {"type": "array", "items": {"type": "string"}},
    },
}

SCHEMAS = {"review": REVIEW_SCHEMA, "drift": DRIFT_SCHEMA,
           "specify": SPECIFY_SCHEMA, "plan": PLAN_SCHEMA}


# --- the rest of the Spec Kit skills (constitution, tasks, analyze, checklist, clarify, ...) ---

SKILL_SPECS = {
    "constitution": {
        "skill": "speckit-constitution",
        "hint": "principles or values for the project",
        "args_required": False,
        "default_args": ("no principles were given: propose a short, concrete constitution that fits "
                         "this project (quality gates, testing, review, docs) and mark every invented "
                         "rule as a proposal for the team to ratify"),
        "writes": ".specify/memory/constitution.md",
    },
    "tasks": {
        "skill": "speckit-tasks",
        "hint": "optional task generation constraints",
        "args_required": False,
        "default_args": "",
        "writes": "specs/<feature>/tasks.md",
    },
    "analyze": {
        "skill": "speckit-analyze",
        "hint": "optional focus areas",
        "args_required": False,
        "default_args": "",
        "writes": "analysis report (non-destructive)",
    },
    "checklist": {
        "skill": "speckit-checklist",
        "hint": "domain or focus area for the checklist",
        "args_required": False,
        "default_args": "requirements quality",
        "writes": "specs/<feature>/checklists/",
    },
    "clarify": {
        "skill": "speckit-clarify",
        "hint": "optional areas to clarify",
        "args_required": False,
        "default_args": "",
        "writes": "specs/<feature>/spec.md (answers encoded)",
        "headless_note": ("Nobody can answer in this run, so do NOT ask questions in the chat. "
                          "Write every clarification question you would ask to {questions_file}, "
                          "one per line, each with why it matters and the default you assumed. "
                          "Then stop."),
    },
    "implement": {
        "skill": "speckit-implement",
        "hint": "optional implementation guidance or task filter",
        "args_required": False,
        "default_args": "",
        "writes": "source code and tests (destructive: it writes files)",
        "dangerous": True,
    },
    "converge": {
        "skill": "speckit-converge",
        "hint": "optional focus",
        "args_required": False,
        "default_args": "",
        "writes": "specs/<feature>/tasks.md (appends remaining work)",
    },
}


def skill_prompt(skill: str, args: str, artifact_dir: Path) -> str:
    """Prompt for a Spec Kit skill that is not specify/plan."""
    spec = SKILL_SPECS[skill]
    given = (args or "").strip()
    body = given or spec["default_args"]
    head = f"/{spec['skill']}" + (f" {body}" if body else "")
    parts = [head, NO_QUESTIONS]
    note = spec.get("headless_note")
    if note:
        parts.append(note.format(questions_file=norm(Path(artifact_dir) / "questions.md")))
    return "\n\n".join(parts)


def skill_prompt_for(harness: str, skill: str, args: str, artifact_dir: Path,
                     egm_root: Path) -> str:
    """Same, for harnesses without slash commands: point at the installed skill file."""
    spec = SKILL_SPECS[skill]
    given = (args or "").strip() or spec["default_args"]
    docs = {"copilot": "docs/copilot.md", "codex": "docs/codex.md"}.get(harness, "docs/claude.md")
    parts = [f"Read {docs} of the Spec Kit project and run the `{spec['skill']}` skill"
             f"{' with: ' + given if given else ''}.", NO_QUESTIONS]
    note = spec.get("headless_note")
    if note:
        parts.append(note.format(questions_file=norm(Path(artifact_dir) / "questions.md")))
    return "\n\n".join(parts)
