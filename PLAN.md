# Plan de mejora de speckfish

Objetivo: una CLI que junte Spec Kit y Elephant-Goldfish Model (EGM) y haga más, en menos pasos.

Estado de partida: 151 tests en verde (`pytest tests`), Claude y Copilot verificados de principio a fin. El problema principal es que el usuario sigue encadenando a mano 6–9 comandos, y los batches (`--all-features`) tienen errores que ni los tests ni la CI detectan.

> Nota: `pytest` sin argumentos recoge también `TokenEconomizer/` (carpeta sin trackear) y da 43 fallos ajenos al proyecto. Con `pytest tests` pasan los 151.

## Fase 0 — Errores que corregir ya

**Estado: hecha** en la rama `fix/phase0-batches` (tests en `tests/test_phase0.py`, 176 en verde, ruff limpio). Pendiente de PR.

Hallazgo adicional: el `speckfish` instalado en editable apuntaba a otra copia (`C:\Users\javie\dev\sdd-lab\speckfish`), así que `pytest` probaba esa y no este repo. Corregido con `pythonpath = ["src"]`.

| # | Error | Dónde | Efecto |
|---|---|---|---|
| 1 | En los batches el nombre del paso lleva la feature (`review-001-x`), y `postprocess` busca `step == "review"` exacto | `runner.py:343`, `steps.py:365` | `review --all-features` nunca lee los blockers: **sale con 0 aunque haya blockers**. El gate de CI no sirve para batches. Igual con `drift-*` y `specify-N`, que tampoco se validan |
| 2 | `do_drift` siempre escribe en `artifacts/drift`, pero `run_step` lo busca en `artifacts/drift-<feature>` | `steps.py:788` | Los drifts de cada feature se pisan entre sí y `--skip-existing` no encuentra el informe |
| 3 | `execute` nombra el log con `step` y no con `label` | `runner.py:86` | En batches de `plan`/`tasks` hay un único `plan.log` sobrescrito, y `res.log` apunta a un fichero que no existe |
| 4 | `specify --split --review` usa siempre el nombre de paso `review` | `steps.py:733` | Cada revisión sobrescribe la anterior en la misma run |
| 5 | Con copilot o codex, `do_specify` y `do_plan` no expanden `@fichero` ni añaden el idioma; además hay una rama muerta con `h.name == "claude"` repetido | `steps.py:476-482`, `515` | `specify @req.md --harness copilot` pasa el texto literal `@req.md` al agente |
| 6 | `_specify_each` usa el slash command de Claude para cualquier harness y no añade el idioma | `steps.py:720` | Con `--split` y codex/copilot los prompts no son los correctos |
| 7 | Al cerrar un agente por inactividad, el resultado solo se da por bueno si hay informes | `runner.py:477` | `specify`, `plan` o `implement` que sí terminaron se marcan como fallidos |
| 8 | `_git_dirty` solo mira el estado al final | `runner.py:299` | En un repo que ya tenía cambios, `implement` "pasa" aunque no escriba nada. Hay que comparar antes y después |
| 9 | Error tipográfico `r"C:\Program Files\Gitin\git.exe"` | `config.py:195` | La ruta de reserva de git en Windows no funciona |
| 10 | `skill_prompt_for` manda leer `docs/copilot.md` "del proyecto Spec Kit", que no existe | `prompts.py:358` | Debe apuntar a `.github/skills/speckit-*/SKILL.md` o `.agents/skills/...` |
| 11 | Opciones ignoradas sin aviso: `review --with-analyze` sin target y `plan --with-clarify --all-features` | `cli.py:566`, `610` | El usuario cree que se ejecutó algo que no se ejecutó |
| 12 | `review` y `drift` sin target usan la feature más reciente por mtime; `plan` y `tasks` usan `.specify/feature.json` | `steps.py:532`, `784` | Se puede revisar una feature distinta de la planificada |
| 13 | Los fallos de cuota/auth se detectan buscando `"429"` y `"credit"` en cualquier salida | `runner.py:216` | Falsos positivos que acaban en exit 3 |

Higiene:

- [ ] `testpaths = ["tests"]` en `pyproject.toml`.
- [ ] Añadir `ruff` a las dependencias de desarrollo y a la CI.
- [ ] Corregir la firma de `execute` (dice 5 valores, devuelve 6).
- [ ] Un test de regresión por cada fila de la tabla.

**Causa común de 1–4:** la misma cadena se usa como tipo de paso y como etiqueta del artefacto. La solución de fondo es pasar `kind` y `label` por separado en toda la cadena (Fase 2).

## Fase 1 — Menos pasos (objetivo del producto)

**Estado: hecha en la rama `feat/phase1-go`** (apilada sobre la Fase 2; 220 tests). Hecho: `go`, `revise`, bucle drift → converge dentro de `go --implement`, `next`, `status`, `--budget`. Por hacer: validarlo con un agente real (todo está probado con agentes simulados) y `--fallback` ya aplica a toda la cadena de `go`.

1. **`speckfish go "<idea|@brief>"`**: un solo comando que recorre el ciclo:
   - `init` si falta.
   - `--split` automático si el brief es grande.
   - specify → clarify (aceptando y registrando los valores por defecto) → plan → tasks → analyze.
   - Revisión EGM.
   - Se detiene antes de `implement`, salvo `--implement --yes`.
   - Luego drift.

   `pipeline` queda como alias. La salida es un resumen, no el volcado JSON actual (`cli.py:658`).
2. **Bucle revisión → corrección** (`revise`): pasa el `consolidated_review_report.md` al agente para que corrija `plan.md`/`tasks.md` y vuelva a revisar, hasta que no haya blockers o se agote `--max-rounds N`. Es lo que más aporta al unir EGM y Spec Kit.
3. **Bucle drift → converge**: el `DRIFT_REPORT` alimenta `converge`, que añade a `tasks.md` lo que falta, y `implement` reanuda. El ciclo se cierra solo.
4. **`speckfish next` / `go --resume`**: mira los artefactos de cada feature y ejecuta el siguiente paso que falta.
5. **`speckfish status`**: une `features`, `runs` y `show`. Muestra fase por feature, blockers de la última revisión, coste acumulado y siguiente comando sugerido.
6. **`--budget` por run**: tope global de dólares o créditos para toda la cadena, además del límite por paso.

## Fase 2 — Arquitectura

**Estado: hecha en la rama `refactor/phase2-kind-label`** (apilada sobre la Fase 0; 191 tests en verde). Hecho: `kind`/`label` separados, `registry.py`, `workspace.py`, `run_batch` único, tabla `HANDLERS`, `Harness.slash_commands`. Queda: que el harness genere el prompt completo (`h.render`) y los `.result.json` por etiqueta en todos los caminos (el de `run_interactive` sigue usando el nombre del paso). Hallazgo: `speckfish review` sin flags salía con 2 en silencio desde #28; arreglado y con test.

- **Registro de pasos**: cada paso declara nombre, requisitos, salidas, schema, validador y clave de timeout. `go` y `next` salen del registro y desaparecen los `if step == ...` de `postprocess`.
- **El harness genera su propio prompt** (`h.render(kind, ...)`): elimina los seis `if h.name == "claude"` de `steps.py`.
- **Un único ejecutor de batches**: hoy hay tres casi iguales (`do_review_batch`, `do_drift_batch`, `do_feature_batch`).
- **Estado indexado por etiqueta**: `state.json` y `*.result.json` por `label`; los `batch*.json` dejan de ser un parche.
- **`cli.main`** (unas 320 líneas de if/elif): tabla de handlers, uno por comando.

## Fase 3 — Más potencia

- Revisión Goldfish ligera del spec antes de planificar (`--stage feature_spec`), opcional dentro de `go`.
- Features en paralelo con límite de concurrencia (ahora todo es en serie).
- `--fallback` aplicado a toda la cadena de `go`, no solo a los batches.
- Dividir `test_smoke.py` (~2.000 líneas) por módulo y añadir tests de extremo a extremo con un harness falso que escriba los artefactos.

## Orden recomendado

1. Fase 0 (1–2 PRs pequeños con tests de regresión).
2. Fase 2: registro de pasos y `kind`/`label` separados.
3. Fase 1 sobre esa base: `go`, `revise`, `next`, `status`.
4. Fase 3.

Hacerlo al revés montaría `go` sobre los batches rotos.
