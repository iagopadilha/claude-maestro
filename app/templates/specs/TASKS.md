# <slug> tasks

**Spec**: spec.md · **Max parallel tasks**: from the ROADMAP header

<!--
Format read by `harness.py tasks waves <slug>` (one block per task, heading `### Tn: title`, `[P]` = may run alongside others):
- Type: impl | wiring. No test type, no test task. No tests in a project that has none.
- Files: what the task edits, comma-separated. Two tasks sharing a file never run in the same wave.
- Deps: T-ids comma-separated, or none. Wiring (DI, routes, composition) goes last and depends on what it wires.
- Reqs: requirement ids from the spec.
- Done when: observable behaviour.
- Status: pending | running | done | failed (written by the loop; leave pending).
Size: Small = 1 to 5 tasks. Replace the example below.
-->

### T1: Create the CSV exporter [P]
**Type**: impl
**Files**: src/export/csv_exporter.py
**Deps**: none
**Reqs**: EXP-01
**Done when**: exporting two rows returns a header line and two data lines
**Status**: pending

### T2: Create the XLSX exporter [P]
**Type**: impl
**Files**: src/export/xlsx_exporter.py
**Deps**: none
**Reqs**: EXP-02
**Done when**: exporting two rows returns a workbook with one sheet and three rows
**Status**: pending

### T3: Register the exporters and the route
**Type**: wiring
**Files**: src/app/composition.py
**Deps**: T1, T2
**Reqs**: EXP-01, EXP-02
**Done when**: `GET /export?format=csv` and `format=xlsx` answer 200 with the file
**Status**: pending
