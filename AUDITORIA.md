# Auditoría de speckfish (v0.2.1): seguridad, rendimiento y mejoras

Fecha: 2026-11-26 · Alcance: `src/speckfish/*` (~4.000 líneas), tests y workflows de `.github/`.

Estado de partida comprobado: **263 tests en verde, 2 omitidos (21 s)** y `ruff check src tests` limpio.
Este documento complementa a `PLAN.md` (Fases 0–3, ya en gran parte hechas). Aquí no se repite lo que
ese plan ya cubre.

Severidad: 🔴 alta · 🟠 media · 🟡 baja.

---

## 1. Resumen

| # | Hallazgo | Tipo | Sev. | Esfuerzo |
|---|---|---|---|---|
| S1 | Los agentes se lanzan con permisos totales (Bash libre, sin sandbox) y leen texto no confiable | Seguridad | 🔴 | M |
| S2 | EGM se clona sin fijar commit y se carga como plugin con permisos totales | Seguridad (cadena de suministro) | 🔴 | S |
| S3 | `.vscode/tasks.json` usa `"type": "shell"` con la entrada del usuario: el shell la expande | Seguridad | 🟠 | S |
| S4 | `init` no añade `.speckfish/` al `.gitignore` del proyecto: los logs completos del agente acaban en git | Seguridad (fuga de datos) | 🟠 | S |
| S5 | Un repo clonado puede traer `vendor/egm` o `.speckfish/config.json` propios y speckfish los usa sin avisar | Seguridad | 🟠 | S |
| S6 | `@fichero` lee cualquier ruta (incluida `~/.ssh/...`) y la envía al proveedor del LLM | Seguridad | 🟡 | S |
| S7 | Workflows: acciones sin fijar a SHA, `contents: write` en todo `release`, `${{ inputs.tag }}` dentro de `run:` | Seguridad CI | 🟡 | S |
| S8 | El prompt va en argv: visible en `ps` y con riesgo de pasar el límite de 32 K de Windows | Seguridad / robustez | 🟡 | M |
| P1 | `json_object_with` es cuadrático: 2 MB de log ≈ 1,5 s, 20 MB ≈ minutos | Rendimiento | 🔴 | S |
| P2 | `status` / `next` / `go` releen todos los `state.json` de todas las runs varias veces por feature | Rendimiento | 🟠 | M |
| P3 | Todo stdout del agente se guarda en memoria y se parsea como JSON 4–5 veces | Rendimiento | 🟠 | M |
| P4 | `proc.wait(timeout=5)`: hasta 5 s de latencia en idle/timeout; 4 tests tardan 5 s cada uno (20 s de 21 s) | Rendimiento | 🟡 | S |
| P5 | `.speckfish/runs/` crece sin límite (no hay retención automática) | Rendimiento / disco | 🟡 | S |
| P6 | `plan_prompt` mete el brief dos veces en el prompt | Coste (tokens) | 🟡 | S |
| B1 | `specify --split --dry-run` sobrescribe `specs/features.json` con `{}` | Bug (pérdida de datos) | 🔴 | S |
| B2 | `Budget` suma USD y créditos de Copilot como si fueran la misma moneda | Bug | 🟠 | S |
| B3 | `state.json` se escribe con leer-modificar-escribir y sin escritura atómica; el `run_id` tiene resolución de segundos | Bug / concurrencia | 🟠 | S |
| B4 | Errores sin capturar: `config.json` corrupto, `SPECKFISH_IDLE_SECONDS=abc`, binario que desaparece (`Popen` → traceback) | Robustez | 🟡 | S |
| B5 | Otros bugs menores (ver §4) | Bug | 🟡 | S |

---

## 2. Seguridad

### S1 🔴 Agentes con permisos totales y entrada no confiable

**Dónde:** `harnesses.py:101-105` (Claude: `--permission-mode acceptEdits --allowedTools Bash PowerShell …`),
`harnesses.py:181-182` (Copilot: `--allow-all-tools --allow-all-paths`), `harnesses.py:311-312`
(Codex: `--dangerously-bypass-approvals-and-sandbox`).

**Riesgo:** cada paso ejecuta un agente que puede lanzar cualquier comando, sin sandbox y sin confirmación.
Lee material que no controlas: briefs `@fichero`, specs, informes EGM y código del repo. Una inyección de
prompt en cualquiera de ellos (por ejemplo en un issue copiado al brief o en una dependencia) basta para
ejecutar código en la máquina o en el runner de CI, con acceso a los tokens del entorno.
`implement` lo agrava porque escribe código y ejecuta la build y los tests.

**Acciones:**
1. Perfil de permisos por *kind* en `registry.py` (`StepSpec.tools`):
   - `review`, `drift`, `analyze`, `clarify` y `split`: solo lectura más escritura en el `artifact_dir`.
     En Claude, `--allowedTools Read Glob Grep Write Agent` y `Write` limitado con `--add-dir` o reglas `Write(<artifact_dir>/**)`.
   - `specify`, `plan` y `tasks`: escritura solo en `specs/` y `.specify/`, y Bash con lista de comandos permitidos (`Bash(git:*)`, scripts de Spec Kit).
   - `implement`: el perfil abierto actual, pero solo con `--yes` y avisando claramente en la salida.
2. Codex: probar de nuevo `--sandbox workspace-write` en versiones recientes. Si sigue roto, exigir
   `--unsafe` explícito (o `config harness.codex.unsafe=true`) en vez de activarlo siempre.
3. En la documentación de CI, recomendar ejecutar en un contenedor desechable sin secretos y no dar
   `GITHUB_TOKEN` con escritura a los jobs que lanzan agentes.
4. Variable `SPECKFISH_ENV_ALLOWLIST`: pasar al agente solo las variables necesarias. Hoy hereda todo
   `os.environ` (`runner.py:102`).

### S2 🔴 EGM sin fijar versión

**Dónde:** `steps.py:163-173` (`git clone --depth 1 https://github.com/google/elephant-goldfish-model`).

**Riesgo:** se descarga la punta de `main` en cada `init` y se carga como plugin (`--plugin-dir`) con los
permisos de S1. Si el repo upstream se compromete, o un commit cambia las skills, la máquina queda expuesta
y los resultados dejan de ser reproducibles.

**Acciones:** fijar `EGM_REF` (tag o SHA) en el código y comprobarlo tras clonar
(`git -C vendor/egm rev-parse HEAD`). Añadir `speckfish init --egm-ref <ref>` para actualizar a propósito
y mostrar el commit cargado en `doctor`.

### S3 🟠 Inyección de shell en las tareas de VS Code

**Dónde:** `editors.py:146-149`, con `"type": "shell"` y `args: [..., "${input:idea}"]`.

**Riesgo:** VS Code entrecomilla los argumentos con comillas dobles. Con bash, `$(…)`, `` `…` `` y `$VAR`
se siguen expandiendo, así que una descripción como `"usa $(whoami)"` ejecuta código. Ocurre también sin
mala intención, al pegar texto con backticks de Markdown.

**Acción:** cambiar a `"type": "process"`, con `command` = ejecutable y `args` = lista. Si `detect_command`
devuelve `python -m speckfish`, separarlo en `command: python` y `args: ["-m", "speckfish", …]`.
Añadir un test que compruebe que ninguna tarea generada es de tipo `shell`.

### S4 🟠 `.speckfish/` acaba en git

**Dónde:** `do_init` (`steps.py:123`) no toca el `.gitignore` del proyecto destino.

**Riesgo:** `.speckfish/runs/*/logs/*.log` guarda la transcripción NDJSON completa: salidas de comandos,
contenido de ficheros leídos y, a veces, variables de entorno. Un `git add -A` la sube al remoto.

**Acción:** en `init`, añadir `.speckfish/` (y `vendor/egm/` si se clonó) al `.gitignore` de forma
idempotente y decirlo en la salida. En `doctor`, avisar si `git check-ignore .speckfish` falla.

### S5 🟠 Configuración y plugins tomados del propio repo

**Dónde:** `config.py:121-134` (`egm_path` prefiere `root/vendor/egm`) y `config.py:72-96` (`config.json` del proyecto).

**Riesgo:** al ejecutar speckfish sobre un repo de terceros (por ejemplo para revisar un PR), un
`vendor/egm` falso se carga como plugin con permisos totales. Un `config.json` puede cambiar `harness`,
`model` o `egm_root`.

**Acción:** guardar en `~/.speckfish/trusted.json` el hash del `plugin.json` y de `skills/` aceptados la
primera vez (como hace `direnv allow`). Si cambian, pedir `speckfish trust` o `--trust`. En CI, `--trust`
explícito.

### S6 🟡 `@fichero` sin restricción de ruta

**Dónde:** `prompts.py:317-343`.

**Riesgo:** `@~/.aws/credentials` o `@/etc/…` se incrusta en el prompt y se envía al proveedor. La entrada
la escribe el usuario, pero las slash commands generadas (`editors.py`) hacen que sea *otro agente* quien
construye la línea (`$ARGUMENTS`).

**Acción:** por defecto, permitir solo rutas dentro de `cfg.root` (resolviendo symlinks) y avisar o
rechazar el resto salvo con `--allow-outside-root`. Usar un delimitador con nonce
(`<<<SPECKFISH-INPUT-{random}`) en vez de `<<<`/`>>>`, que el propio contenido puede cerrar.

### S7 🟡 Endurecer los workflows

- Fijar `actions/checkout`, `actions/setup-python` y el resto a SHA, y añadir `.github/dependabot.yml`
  (ecosistema `github-actions` y `pip`).
- `release.yml`: `permissions: contents: read` global y `contents: write` solo en el job `release`.
- `release.yml`, paso *Check the tag*: `TAG="${{ github.event.inputs.tag || github.ref_name }}"` se
  interpola dentro del script. Pasarlo por `env:` como ya se hace en los demás pasos.
- `tests.yml`: no declara `permissions:`. Añadir `contents: read`.
- `pr.yml`: `pip install ruff` sin versión. Usar `pip install -e ".[dev]"` o fijar la versión, para que la
  CI y el entorno local den el mismo resultado.
- Publicación: añadir attestations (`actions/attest-build-provenance`) junto a `SHA256SUMS.txt`.

### S8 🟡 El prompt como argumento de línea de comandos

**Dónde:** `harnesses.py:101`, `181` y `311`.

**Riesgo:** el prompt (hasta 12 KB inline, el doble en `plan`, más el JSON schema y el `CODEX_FILE_HINT`)
es visible en `ps` para otros usuarios de la máquina y se acerca al límite de 32.767 caracteres de
`CreateProcess` en Windows.

**Acción:** pasarlo por stdin cuando la CLI lo admita (`claude -p` lee stdin; `codex exec -` también).
`StepInvocation.stdin` ya existe pero no se usa. Si no, escribirlo en un fichero temporal `0600` dentro
del `artifact_dir` y pasar la ruta.

---

## 3. Rendimiento

### P1 🔴 `json_object_with` cuadrático

**Dónde:** `steps.py:596-626`. Por cada `{` del texto ejecuta `decoder.raw_decode(text[start:])`, que
**copia el resto del texto** y vuelve a parsear objetos anidados que ya estaban dentro de uno decodificado.

**Medido:** 543 KB → 0,04 s; 2,1 MB → 1,46 s (4× más texto, 36× más tiempo). Un log de `split` con
subagentes llega fácilmente a decenas de MB.

**Acción:**
```python
obj, end = decoder.raw_decode(text, start)   # sin slice
idx = end                                     # saltar lo ya decodificado
```
Además: recorrer primero solo las líneas NDJSON (`type == "result"` / mensajes del asistente) y usar el
escaneo carácter a carácter como último recurso. Añadir un test de rendimiento (5 MB en < 1 s).

### P2 🟠 Lecturas repetidas de `state.json`

**Dónde:** `flow.py:34-49` (`latest_record`), `flow.py:96-112` (`feature_state`), `flow.py:114`
(`next_step` vuelve a llamar a `feature_state`), `flow.py:148` (`status_rows`), `flow.py:158`
(`total_cost`) y `steps.py:344-361` (`review_already_done`, glob sobre todas las runs por feature).

**Coste:** `status` hace unas `F × (2 × 3) × R` lecturas y parseos JSON (F = features, R = runs), y
`review_is_current` se calcula dos veces. `advance()` llama a `next_step` en cada iteración del bucle.
Con 10 features y 200 runs son unos 12.000 parseos por `status`. Además, la regex de `latest_record` se
compila dentro del bucle de runs.

**Acción:**
1. `RunIndex`: lee cada `state.json` una vez por invocación (o con caché por `mtime`) y construye
   `{(kind, feature): [entries]}`. Pasarlo a `feature_state`, `next_step` y `total_cost`.
2. `next_step(cfg, feature, state=None)`: reutilizar el estado ya calculado en `status_rows`.
3. A medio plazo, un índice incremental `.speckfish/index.json` que actualiza `record_state` y elimina el
   escaneo de runs.

### P3 🟠 stdout completo en memoria y parseado varias veces

**Dónde:** `runner.py:108-129` acumula todo en `out_chunks`; luego `parse_output`, `error_kind` (dos veces,
una dentro de `human_error`), `_json_payloads` y `inventory_payload` (que además relee el log del disco)
vuelven a recorrerlo. `ClaudeHarness.parse_output` empieza con un `json.loads` del stdout entero, que
falla siempre en stream-json.

**Acción:** que el hilo `pump` extraiga sobre la marcha lo que se necesita: el último evento `result`,
`rate_limit_event`, costes y modelo. Puede ser un `harness.observe(line)` que el `Progress` ya hace a
medias. Guardar en memoria solo una cola acotada (`collections.deque(maxlen=…)`) para el `raw_tail`. En
`human_error`, calcular `payloads` una vez y pasarlo a `error_kind`.

### P4 🟡 Sondeo de 5 s

**Dónde:** `runner.py:146`. El timeout y el cierre por inactividad se evalúan cada 5 s, y los 4 tests más
lentos tardan exactamente 5 s (20 de los 21 s de la suite).

**Acción:** `SPECKFISH_POLL_SECONDS` (por defecto 1 s, 0,1 en tests) o un `threading.Event` que el hilo
`pump` active al ver el evento final. La suite bajaría a ~2 s.

### P5 🟡 Retención de runs

`runs --clean` existe, pero es manual. Añadir `config runs.keep=N` (por ejemplo 50) y limpiar al terminar
cada comando. Opcional: comprimir con gzip los `.log` de runs antiguas.

### P6 🟡 Brief duplicado en `plan`

**Dónde:** `prompts.py:58-63`. `"/speckit-plan {stack}\n\nStack and constraints: {stack}"` mete dos veces
el contenido de `@stack.md` (hasta 24 KB y el doble de tokens de entrada). Dejar una sola copia.

---

## 4. Bugs y robustez

### B1 🔴 `--dry-run` borra el corte de features

`steps.py:696-698` escribe `specs/features.json` **antes** de comprobar `dry_run`. En dry-run `structured`
es `None`, así que el payload es `{}` y se pierde un corte aprobado con `--inventory-only`, que es justo lo
que luego usa `--only`. Mover la escritura detrás del `if dry_run`, o no escribir si `features` está vacío.
Añadir un test de regresión.

### B2 🟠 `Budget` mezcla monedas

`flow.py:403-414` suma `res.cost` sin mirar `cost_unit`. Con `--fallback copilot`, 30 créditos se cuentan
como 30 USD y la cadena se corta en cuanto entra el fallback. Guardar el gasto por unidad y aplicar el
límite solo a la del harness principal, o aceptar `--budget 5usd,90credits`. Además, el presupuesto solo se
comprueba *antes* de cada paso: pasar el resto (`limit - spent`) como `budget` del siguiente
`run_step` (Claude admite `--max-budget-usd`).

### B3 🟠 Escrituras de estado no atómicas y `run_id` con colisiones

- `record_state` (`runner.py:335-354`), `Config.save` y los `batch-*.json` usan `write_text` directo. Una
  interrupción a mitad deja JSON truncado, y después `json.loads` lanza excepción (en `run_summary` no se
  captura). Hay que escribir a `*.tmp` y luego `os.replace`.
- `new_run_id()` tiene resolución de segundos: dos `speckfish` lanzados a la vez (por ejemplo dos jobs de
  CI sobre el mismo checkout) comparten carpeta y pisan `state.json`. Añadir un sufijo aleatorio
  (`20261126-101500-a3f9`) y un lock (`state.json.lock`) en `record_state`.

### B4 🟡 Excepciones que acaban en traceback

- `Config.load`: un `config.json` corrupto lanza `JSONDecodeError`, que `main` no captura (solo captura
  `SystemExit`).
- `int(os.environ["SPECKFISH_IDLE_SECONDS"])` y `SPECKFISH_RESULT_GRACE` dan `ValueError` con valores no
  numéricos.
- `execute`: si `Popen` falla (`FileNotFoundError` o `WinError 193`), la barra de progreso ya arrancó y no
  se para, y el error sale como traceback. `run_interactive` sí lo maneja: unificar.
- `run_summary` y `cmd_runs` fallan con un `state.json` corrupto.

Acción: un `try/except (OSError, ValueError)` en esos puntos, con un mensaje claro y `EXIT_ENV`.

### B5 🟡 Menores

- `workspace._git_porcelain` llama a `git` a pelo y no a `find_git()`, así que en Windows con App Control
  vuelve el WinError 5 que `find_git` resuelve.
- `_GIT_NOISE` filtra por subcadena: cualquier ruta que contenga `/bin/` (por ejemplo `src/bin/main.rs`)
  o `.gitignore` deja de contar como trabajo real en `implement`. Comparar por prefijo o segmento de ruta.
- `find_feature`: `name.startswith(raw)` elige la primera coincidencia en silencio (`--feature 0`). Si hay
  varias, debe dar error de ambigüedad.
- `CODEX_FILE_HINT` (PowerShell, ibm850…) se añade también en Linux y macOS. Condicionarlo a `is_windows()`.
- `CopilotHarness.build` ignora el parámetro `budget`: solo lee `cfg.budgets`.
- `runner.py:175-176`: `code = 0 if code is None else code` es código muerto (`code` nunca es `None` ahí).
- `do_specify(..., review=True)` descarta el resultado de la revisión (`steps.py:484`): ni se devuelve ni
  afecta al código de salida.
- `do_doctor` llama dos veces a `latest_feature` (`steps.py:216`).
- `clean_runs(keep<0)` borra runs inesperadas (`runs[:-keep]` con `keep` negativo). `_non_negative` lo
  valida en la CLI, pero no en la API.
- `active_feature`/`feature_dir` aceptan rutas absolutas o `../` en `.specify/feature.json`, un fichero que
  escribe el agente. Restringirlas a `cfg.root`.

---

## 5. Mejoras de calidad

1. **Cobertura y tipos:** añadir `pytest-cov` (umbral inicial ~80 %) y `mypy --strict` gradual empezando
   por `runner.py` y `registry.py`. Los `Validator = Callable[["object", Ctx], None]` deberían tipar
   `StepResult`.
2. **Reglas de ruff:** activar `S` (bandit), `PTH`, `RUF` y `PERF` en `pyproject.toml`. `S` habría marcado
   S3 y S8.
3. **`cli.py` (1.200 líneas):** separar los `summarize`/`*_summary` en `render.py` y dejar `cli.py` en el
   parser y los handlers.
4. **`run_interactive`:** usar `label` y `kind` como `run_step`. Es lo que queda pendiente de la Fase 2 de
   `PLAN.md`.
5. **Logs estructurados:** guardar junto a cada `.log` un `.meta.json` con argv (sin el prompt), env
   filtrado, versión del harness y commit de EGM, para poder reproducir los pasos.
6. **Seguridad documentada:** un `SECURITY.md` con el modelo de amenazas (S1/S5), cómo informar de
   vulnerabilidades y recomendaciones para CI.
7. **Higiene del repo:** quitar `src/speckfish/__pycache__` del árbol de trabajo (ya ignorado) y comprobar
   en CI que el wheel no incluye `.pyc`.

---

## 6. Plan de acción

### Sprint 1: correcciones rápidas de alto impacto (1–2 PRs, ~1 día)
- [ ] **B1**: no sobrescribir `features.json` en dry-run, con test.
- [ ] **P1**: `json_object_with` lineal, con test de rendimiento.
- [ ] **S3**: tareas de VS Code `type: process`, con test.
- [ ] **S4**: `.speckfish/` en el `.gitignore` del proyecto durante `init`, más aviso en `doctor`.
- [ ] **S2**: fijar `EGM_REF` y mostrar el commit en `doctor`.
- [ ] **B4**: capturar config corrupta, variables de entorno no numéricas y fallo de `Popen`.
- [ ] **P4**: intervalo de sondeo configurable (suite de 21 s a ~2 s).

### Sprint 2: estado y rendimiento (~2–3 días)
- [ ] **B3**: escritura atómica (`os.replace`), `run_id` con sufijo y lock en `record_state`.
- [ ] **P2**: `RunIndex` compartido por `status`, `next`, `go` y `review_already_done`.
- [ ] **P3**: extracción incremental en `pump`, cola acotada y `payloads` calculados una sola vez.
- [ ] **B2**: `Budget` por unidad y presupuesto restante pasado al paso siguiente.
- [ ] **P6** y **B5**: pequeños arreglos agrupados en un PR `fix: minor robustness`.

### Sprint 3: modelo de permisos (~3–5 días, requiere probar con agentes reales)
- [ ] **S1**: `StepSpec.tools` por *kind*, perfiles de solo lectura para review/drift/analyze, Codex con
      `--unsafe` explícito y allowlist de variables de entorno.
- [ ] **S5**: confianza en `vendor/egm` y `config.json` por hash (`speckfish trust`).
- [ ] **S6**: `@fichero` limitado a `cfg.root` y delimitador con nonce.
- [ ] **S8**: prompt por stdin o fichero temporal.
- [ ] Verificar de extremo a extremo con claude, copilot y codex (`--dry-run` y una ejecución real),
      siguiendo el checklist de `pr-review.yml`.

### Sprint 4: CI y calidad (~1–2 días)
- [ ] **S7**: acciones fijadas a SHA, Dependabot, permisos mínimos, `inputs.tag` por `env` y attestations.
- [ ] Ruff `S`/`PERF`/`RUF`, `pytest-cov` con umbral y `mypy` gradual.
- [ ] `SECURITY.md` y la sección de seguridad del README (cómo ejecutar en CI de forma aislada).
- [ ] Retención automática de runs (**P5**) y `.meta.json` por paso.

### Criterios de aceptación globales
- Todos los hallazgos 🔴 tienen un test de regresión.
- La suite sigue sin necesitar red ni CLIs de agente, y tarda menos de 5 s.
- `speckfish status` con 20 features y 500 runs responde en menos de 1 s.
- Ningún paso que no sea `implement` puede ejecutar Bash arbitrario sin opt-in explícito.
