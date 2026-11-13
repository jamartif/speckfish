"""Plantillas de prompt, perfiles de criterios y esquemas de salida estructurada."""
from __future__ import annotations

from pathlib import Path

from .config import norm

NO_QUESTIONS = ("Ejecución headless sin usuario: nadie puede responder preguntas. "
                "No pidas confirmación, asume valores razonables y termina la tarea.")

# --- perfil de criterios: evita el ruido de formato al revisar artefactos de Spec Kit ---
PROFILES = {
    "speckit": (
        "CRITERIOS DE REVISIÓN ESPECÍFICOS PARA SPEC KIT (aplícalos sobre el estándar): "
        "este documento es un artefacto de GitHub Spec Kit, no un feature spec aislado. "
        "No marques como Blocker ni Major la ausencia de: 'Alternatives Considered', "
        "'Risks & Mitigations', 'Problem Statement', 'Goals & Non-Goals', 'Success Criteria'. "
        "Esas secciones viven en spec.md o not en el documento revisado, y su ausencia aquí es "
        "correcta por diseño de la plantilla. En lugar de eso evalúa: coherencia con spec.md, "
        "decisiones técnicas justificadas, mecanismos explicados (atomicidad, configuración, "
        "códigos de salida, errores), estrategia de pruebas y riesgos técnicos concretos del "
        "diseño elegido."
    ),
    "none": "",
}


def specify_prompt(idea: str) -> str:
    return f"/speckit-specify {idea.strip()}\n\n{NO_QUESTIONS}"


def plan_prompt(stack: str) -> str:
    stack = (stack or "").strip()
    extra = f"Stack y restricciones: {stack}\n\n" if stack else ""
    return f"/speckit-plan {stack}\n\n{extra}{NO_QUESTIONS}"


def review_prompt(target: str, artifact_dir: Path, profile: str = "speckit",
                  stage: str = "uninterrupted") -> str:
    parts = [
        f"/egm:egm-review {norm(target)} Step={stage}.",
        NO_QUESTIONS + " No hay contexto Goldfish adicional (lista vacía).",
        f"EGM artifact directory: {norm(artifact_dir)}",
    ]
    p = PROFILES.get(profile, "")
    if p:
        parts.append(p)
    return "\n\n".join(parts)


def drift_prompt(target: str, artifact_dir: Path) -> str:
    return "\n\n".join([
        f"/egm:egm-drift-report {norm(target)}.",
        NO_QUESTIONS,
        f"EGM artifact directory: {norm(artifact_dir)}",
    ])


# --- prompts para harness sin slash-commands (copilot/codex) ---

def egm_prompt_for(harness: str, kind: str, target: str, artifact_dir: Path,
                   egm_root: Path, profile: str = "speckit", stage: str = "uninterrupted") -> str:
    """Copilot/Codex no resuelven `/egm:...`: se pide leer el SKILL.md del paquete."""
    docs = {"copilot": "docs/copilot.md", "codex": "docs/codex.md"}.get(harness, "docs/claude.md")
    skill = {"review": "egm-review", "drift": "egm-drift-report",
             "brainstorm": "egm-brainstorm", "draft": "egm-draft"}[kind]
    head = [f"EGM package root: {norm(egm_root)}",
            f"Read that package's {docs} and run {skill} on {norm(target)}."]
    if kind == "review":
        head[1] = (f"Read that package's {docs} and run {skill} on {norm(target)} "
                   f"with Step={stage}.")
        head.append(NO_QUESTIONS + " No hay contexto Goldfish adicional (lista vacía).")
    else:
        head.append(NO_QUESTIONS)
    head.append(f"EGM artifact directory: {norm(artifact_dir)}")
    p = PROFILES.get(profile, "")
    if p and kind == "review":
        head.append(p)
    return "\n\n".join(head)


# --- esquemas de salida estructurada (claude --json-schema) ---

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
