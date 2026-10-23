# Loop Harness app

A local control panel for **loop engineering** and **graph engineering**. It opens or creates projects, generates and
approves the roadmap, runs the `/orchestrator` loop and shows everything the loop writes to `.specs/` (the roadmap as a
dependency graph, turns, agents, specs, questions, validations, cost and limits) without opening `.md` files one by one.
It runs on your machine and uses your own Claude subscription through the `claude` CLI (no API token). Every `claude -p` it starts runs isolated from your Claude profile (see `docs/ARCHITECTURE.md`, "Runtime isolation").

- **Plugin skill** (`../plugin/skills/orchestrator/`): the brain. The prompt of every turn (`SKILL.md`, `references/`)
  and the hook `hooks/guard.py`.
- **This app**: the runtime. Loop engine (`backend/harness_backend/engine/`), a layered HTTP API and the front end.

## Run
```
python harness.py install             # once: writes ~/.loop-harness/config.json (the skill finds the app through it)
python harness.py serve               # starts the panel (projects hub) and opens the browser
python harness.py serve --project /path/to/repo
python harness.py doctor              # diagnostics: skill, guard, claude on PATH, notification channels
python harness.py doctor --access     # what a claude -p of the app can reach: MCP, skills, bundle, probe, guard.log (read only)
python harness.py demo                # the panel on a fake project; no claude, nothing of yours touched
python harness.py check <repo>        # loop preflight (the same as the Check button)
python harness.py loop <repo>         # runs the loop from the terminal
```
Python 3.10+, standard library only. No npm: the front end is ES modules served by the back end.

## Tests
```
python -m unittest discover -s backend/tests -t backend
```

## Docs
- [`../README.md`](../README.md): the repository overview (plugin + app).
- [`docs/HOW_TO_USE.md`](docs/HOW_TO_USE.md): the loop step by step, `doctor --access`, the isolation kill switch.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): layers, modules, contracts and the hub/projects API.
- [`docs/API.md`](docs/API.md): the per-project panel API (`/p/<id>/api/...`).
- [`docs/MONITOR.md`](docs/MONITOR.md): how the panel starts, lives and stays safe, and the guard allowlists and audit lines.
- [`docs/SKILL_BRIDGE.md`](docs/SKILL_BRIDGE.md): how the plugin skill finds and uses the app.
