# speckfish 🐟

**Orquestador headless de [Spec Kit](https://github.com/github/spec-kit) + [Elephant-Goldfish Model (EGM)](https://github.com/google/elephant-goldfish-model) sobre CLIs de agente.**

`speckfish` convierte el flujo "especifica → planifica → revisa con Goldfish" en comandos
ejecutables desde terminal y desde CI. Un subproceso del agente por paso, artefactos por
ejecución, veredicto machine-readable y códigos de salida para automatizar.

```
speckfish pipeline "CLI de tareas en Python" --stack "solo stdlib" --gate
```

## Qué resuelve

- **Spec Kit** genera `spec.md` y `plan.md`, pero nadie los revisa con criterio crítico.
- **EGM** revisa con subagentes "goldfish" de contexto limpio, pero su flujo es interactivo.
- `speckfish` los une y ejecuta en modo no interactivo, guardando todo lo que el agente produce.

## Estado (verificado con ejecuciones reales)

| Harness | Estado | Evidencia |
|---|---|---|
| **Claude Code** | ✅ Verificado end-to-end | `specify` → `plan` → `egm-review` (7 subagentes vía `Agent`) → `egm-drift-report`; veredicto con `--json-schema`; ~$0.85-1.04 por review |
| **GitHub Copilot CLI** | ✅ Verificado end-to-end | review completa: **10 subagentes**, 0 blockers con perfil `speckit`, 8 majors; 4.9 AI credits y ~7 min (413 s); drift en 0.63 créditos / 64 s |
| **Codex CLI** | ⚠️ Adaptador sin verificar | `codex exec`; requiere instalación y login. Revisa `docs/codex.md` de EGM |

`speckfish` cuenta los subagentes lanzados en cada ejecución (Claude: `subagent_stats.spawned`;
Copilot: eventos `subagent.started`) y lo guarda en el resultado del paso: sirve como prueba de
que la revisión fue un Goldfish review real y no una revisión en el mismo contexto.

EGM avisa de que su adaptador de Copilot es **experimental** (aislamiento de revisores no
verificado por Google). `speckfish` no arregla eso: lo documenta.

## Instalación

```bash
# con uv (recomendado)
uv tool install git+https://github.com/jamartif/speckfish

# o desde el clon
git clone https://github.com/jamartif/speckfish && cd speckfish
uv venv && uv pip install -e .
```

Requisitos: Python ≥ 3.10. Y el CLI del agente que vayas a usar:

| Harness | Instalación | Login |
|---|---|---|
| Claude Code | `npm i -g @anthropic-ai/claude-code` | `claude` (una vez) |
| Copilot CLI | `npm i -g @github/copilot` | `copilot login` |
| Codex CLI | `npm i -g @openai/codex` | `codex login` |

Spec Kit se descubre solo: usa el binario `specify` si funciona y, si está bloqueado por
políticas de Windows, cae a `uv tool run --from specify-cli specify`.

## Uso

```bash
speckfish doctor                    # entorno, harness, specify y EGM
speckfish init                      # git init + specify init + clona EGM en vendor/egm
speckfish specify "una CLI de tareas en JSON"
speckfish plan "Python 3.12, solo stdlib"
speckfish review                    # revisa plan.md de la última feature
speckfish review --profile none     # revisión literal contra el estándar de EGM
speckfish drift                     # drift entre spec y código
speckfish pipeline "idea" --stack "..." --gate --with-drift
speckfish runs && speckfish show
```

Flags globales (antes o después del subcomando): `--root`, `--harness claude|copilot|codex`,
`--json`, `--quiet`, `--dry-run`.

### Códigos de salida

| Código | Significado |
|---|---|
| `0` | paso correcto / revisión sin blockers |
| `1` | revisión con blockers |
| `2` | error de ejecución (timeout, presupuesto, informe ausente) |
| `3` | entorno incompleto (`doctor` falla) |

## Cómo funciona

1. **Un subproceso por paso.** `claude -p ...`, `copilot -p ...` o `codex exec ...`. Nunca se
   encadenan pasos dentro del mismo contexto: cada uno tiene que escribir su artefacto.
2. **Artefactos aislados por ejecución.** Todo va a `.speckfish/runs/<run-id>/`:
   `logs/<paso>.log`, `artifacts/<paso>/` (aquí escribe EGM sus informes, vía
   `EGM artifact directory:`), `state.json` y `<paso>.result.json`.
   Esto evita que EGM sobrescriba sus informes con nombres fijos.
3. **Perfil `speckit`.** Sin él, EGM juzga un `plan.md` contra su estándar de feature spec y
   marca como Blocker cosas que son plantilla de Spec Kit (falta *Alternatives Considered*,
   *Risks & Mitigations*…). El perfil los declara fuera de alcance y redirige la crítica a
   coherencia, mecanismos y riesgos técnicos. Medido: **2 Blockers → 0**, mismo coste.
4. **Veredicto estructurado.** Con Claude se usa `--json-schema` (`structured_output`:
   `verdict`, `blockers[]`, `majors`, `nits`, `reports[]`). Con Copilot/Codex se parsea el
   informe consolidado en Markdown (tablas o viñetas).
5. **Límites de coste por paso.** `--max-budget-usd` (Claude) y `--max-ai-credits` (Copilot).
   Timeout de reloj propio con kill del árbol de procesos: ningún CLI acepta `--timeout`
   ni `--max-turns`, y `--print` puede quedarse colgado.
6. **EGM no se instala globalmente.** Se pasa `--plugin-dir` apuntando a `vendor/egm`, así que
   no se tocan los settings del usuario y la versión queda fijada en tu repo.

## Coste observado (documento de ~75 líneas)

| Paso | Claude Code | Copilot CLI |
|---|---|---|
| specify | ~$0.26 / 49 s | — |
| plan | ~$0.21 / 46 s | — |
| review (perfil speckit) | ~$1.0 / 4-5 min | 4.9 AI credits / ~7 min (10 subagentes) |
| drift | ~$0.25 / 35 s | 0.63 AI credits / 64 s |

Cifras de `total_cost_usd` / AI credits reportados por cada CLI; con suscripción no se
facturan igual. Mide siempre con `speckfish show`.

## Limitaciones conocidas

- El `structured_output` es un **contrato blando**: si el modelo no lo respeta, se cae al
  parseo del Markdown.
- El informe de EGM es Markdown de formato libre: los recuentos son heurísticos.
- EGM puede saltarse la secuencialidad que promete `Step=uninterrupted` (hemos visto lanzar
  la etapa 6 en paralelo con la 5): guarda los informes y no te fíes de un único veredicto.
- `egm-brainstorm` / `egm-draft` corren, pero son conversacionales: sin humano producen
  hipótesis sin validar. Úsalos como generador de borradores.
- Modo `pipeline` encadena; `--gate` para y no continúa si hay blockers.

## Tests

```bash
uv pip install pytest && python -m pytest -q
```

Sin red y sin invocar CLIs de agente (solo `--dry-run`).

## Licencia

MIT. Spec Kit es de GitHub y EGM de Google (Apache-2.0); este orquestador no los modifica,
solo los invoca.
