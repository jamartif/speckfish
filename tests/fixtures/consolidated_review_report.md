# Informe de revisión consolidado

**Documento**: `specs/001-todo-cli/plan.md` (76 líneas)
**Modo**: ejecución ininterrumpida (headless), sin contexto Goldfish adicional. El enlace `spec.md` se añadió como contexto.
**Etapas ejecutadas**: 5, 6, 7 y 8. **Etapa 9 omitida**: el documento no tiene sección de plan de implementación detallado (`DetailedRange = N/A`).
**Remediación**: no se aplicó ninguna edición a `plan.md`; este informe solo recoge hallazgos.

Informes de etapa: `zoom_out_report.md`, `comprehension_report.md`, `critic_report.md`, `intermediate_zoom_out_report.md`.

## Veredicto

El plan es simple y no tiene problemas de fondo de diseño, pero **no cumple el estándar de feature spec**: faltan alternativas y riesgos, y varios puntos centrales (atomicidad, fichero corrupto, configuración del fichero) se afirman sin explicar el mecanismo. La etapa 8 advierte que no hay que cubrir esos huecos con documentación excesiva.

## Acciones consolidadas

### 🔴 Blockers

| # | Acción | Cómo hacerlo sin sobrecargar |
|---|--------|------------------------------|
| A1 | Añadir "Alternatives Considered" | Una o dos frases: SQLite se descarta por FR-009 (legible por humanos); `click`/`typer` por la regla de cero dependencias. Sin JSON Lines ni "módulo único". |
| A2 | Añadir "Risks & Mitigations" | Tres riesgos: (1) fichero corrupto o esquema inválido, incluido `next_id` menor que el máximo `id` (FR-011/FR-012): abortar sin escribir; (2) fallo de escritura: temporal en el mismo directorio y `os.replace`, con la promesa acotada; (3) codificación: UTF-8 explícito. Resto a Non-Goals. |

### 🟡 Major

| # | Acción |
|---|--------|
| A3 | Conservar solo uno de `--file` y `TODO_FILE` (elimina la ambigüedad de precedencia). Si se mantienen ambos, definir el orden y el comportamiento si el fichero no existe. |
| A4 | Describir en prosa el flujo cargar → mutar → guardar atómico y enlazar `data-model.md` y `contracts/cli.md` (esquema JSON, `next_id`, códigos de salida). Verificar que esos ficheros existen. |
| A5 | Reescribir Constitution Check: "No evaluado: la constitución no tiene principios ratificados". Eliminar la reevaluación, la "comprobación autoimpuesta" y los restos de plantilla (Fases 0/1, texto en inglés). |
| A6 | Acotar las garantías absolutas ("sin pérdida de datos"): "no sobrescribe un fichero que no se pueda leer (FR-012)". Unificar "errores explicativos" con el "error claro" del spec. |
| A7 | Definir Non-Goals por referencia al spec e incluir las exclusiones: concurrencia, `fsync`, symlinks y permisos, más de 1.000 tareas. Sin Problem Statement completo. |
| A8 | Unificar el objetivo de rendimiento a "hasta 1.000 tareas" y verificarlo con una prueba de 1.000 tareas. |
| A9 | Completar la estrategia de pruebas: mapear los casos límite (fichero corrupto, ID no reutilizado, UTF-8, subcomando desconocido) y decidir `main()` en proceso con un caso de humo con `subprocess`. |

### 🟢 Nit

- A10: eliminar o reducir a una línea "Complexity Tracking".
- A11: simplificar "Primary Dependencies" a "solo stdlib" y quitar la repetición de "sin dependencias externas".
- A12: citar FR-012 junto a la escritura atómica en el Summary.
- A13: declarar que no se crearán más capas que `cli`, `models` y `storage`; sustituir "separación mínima" por una descripción verificable.
- A14: unificar el formato de metadatos (líneas 3 y 5) y explicar o quitar `/speckit-tasks` y el prefijo `001`.
- A15: enlazar los artefactos de la lista de documentación.

## Decisiones que rechazan hallazgos previos

- Política de versión mínima de Python (Critic #9): "Python 3.12" ya la fija.
- Locks de concurrencia y `fsync`: fuera de alcance por el supuesto de usuario único.
- Revisor de inventiva: sin falsos compromisos; no se requiere acción.

## Matriz de trazabilidad

| Acción | Etapa 5 | Etapa 6 | Etapa 7 | Etapa 8 |
|--------|---------|---------|---------|---------|
| A1 | | | design #1, structural #1 | hallazgo 1 |
| A2 | | #2 | design #2, structural #2 | hallazgo 2 |
| A3 | zoom-out #1 | #3 | style #4 | hallazgo 4 |
| A4 | zoom-out #6 | #2, #5, #6 | design #4, structural #4 | hallazgo 3 |
| A5 | | #4 | design #5, style #1 | hallazgo 5 |
| A6 | zoom-out #4 | #2 | style #2 | hallazgo 2 |
| A7 | | #1 | structural #3 | resumen |
| A8 | zoom-out #5 | #8 | design #3, style #3 | hallazgo 7 |
| A9 | zoom-out #6 | #9 | design #7, style #10 | hallazgo 8 |
| A10 | | #10 | structural #5, style #8 | hallazgo 6 |
| A11 | zoom-out #3 | | style #6 | |
| A12 | zoom-out #4 | | | |
| A13 | zoom-out #2 | | style #7 | |
| A14 | | #7 | structural #8, style #9 | |
| A15 | zoom-out #6 | #5 | structural #7 | |
