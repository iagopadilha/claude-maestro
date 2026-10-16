# Guard hook: what it blocks and how to add your own rules

`guard.py` is a Claude Code `PreToolUse` hook. Before each tool call it reads the hook payload (JSON on stdin). It either lets the call through (exit 0) or blocks it (exit 2, with the reason on stderr). Claude shows that reason to the agent, and the agent is told to return `NEEDS_CONFIRM` instead of looking for a way around it.

It is deterministic: it does not depend on the model obeying a prompt. Standard library only, Python 3.10+.

## Where it runs

The guard only acts on this plugin's orchestrator and its subagents. Your other Claude Code sessions, skills and agents are not affected.

| Registered in | Command | Applies to |
|---|---|---|
| `plugin/hooks/hooks.json` | `guard.py --profile auto` | the plugin's subagents. `auto` picks the profile from the payload's `agent_type`; any other agent and the main session exit 0 at once. |
| `plugin/skills/orchestrator/SKILL.md` (frontmatter) | `guard.py --profile orchestrator` | the chat while the `/orchestrator` skill is running |

| Profile | Agents | In short |
|---|---|---|
| `orchestrator` | the `/orchestrator` chat | may commit (files by name) and write loop state; never pushes, never touches production, never sends tickets or messages |
| `executor` | `executor-back`, `executor-front`, `executor-generic` | edits code only inside its feature's paths; no write git, no installs, no state files |
| `spec` | `spec-writer`, `spec-reviewer`, `brownfield-mapper` | writes specs; no write git, no state files |
| `verifier` | `verifier`, `verifier-flow` | reads and runs checks; no write git, no state files |

It checks the `Bash` and `PowerShell` tools on the **whole command text**, so `python -c "..."`, `$(...)` and quoted commands are covered. It also checks shell writes (`>`, `>>`, `tee`, `sed -i`, `Set-Content`, `Add-Content`, `Out-File`), `Write`/`Edit`/`NotebookEdit` paths, `Read`/`Glob`/`Grep` inputs for credentials, and `mcp__*` tool names.

## Built-in rules

From `PROFILES` and `HARD` in `guard.py`. O = orchestrator, E = executor, S = spec, V = verifier.

| Rule | Blocks | Profiles | Hard |
|---|---|---|---|
| `git-push` | any `git push`, including one hidden in `python -c`, `$(...)` or a `["git", "push"]` list | O E S V | |
| `push-protected` | a push naming `developer`, `develop`, `main`, `master`, `release`, `production`, `dev` or your integration branch (`LOOP_INTEGRATION_BRANCH` / `LOOP_PR_BASE`), or a bare `git push` while on one of them | every push, any profile | **yes** |
| `push-force` | `--force`, `--force-with-lease`, `-f`, `+refspec` | every push, any profile | **yes** |
| `approval-write` | writing `ROADMAP-APPROVED.json` (only your click in the panel writes it) | O E S V | **yes** |
| `panel-trigger` | calling the panel endpoints that need your click (start or check the loop, approve the roadmap, act, answer, resume, instruct) | O E S V | **yes** |
| `pr-release` | `gh pr`, `release`, `repo`, `gist`, `workflow` other than `view`, `list`, `diff`, `status`, `checks` | O E S V | |
| `prod-write` | write SQL through a database MCP tool whose name contains a production hint (`GUARD_PROD_HINTS`, default `prod,production`), or any write-verb MCP tool on such a server | O E S V | |
| `outbound-msg` | ticket, issue, chat, mail or comment MCP tools with a create, update, send or post verb | O E S V | |
| `secrets` | reading, writing or running anything on `.secrets/`, the plugin's `shared/.env`, `~/.loop-harness/.env` or `app.json`, or SSH private keys | O E S V | |
| `project-deny` | **your** `deny_bash_regex` and `deny_path_regex` (see the cookbook) | O E S V | |
| `git-add-all` | `git add .`, `git add -A`/`--all`, `git commit -a` | O | |
| `git-write` | any git subcommand except `status`, `diff`, `log`, `show`, `rev-parse`, `ls-files`, `blame`, `describe`, `grep`, `shortlog` | E S V | |
| `dev-write` | write SQL through a non-production database MCP tool | E S V | |
| `state-write` | writing `ROADMAP.md`, `STATE.md`, `RULES.md`, `phases/`, `archive/` or anything under `.specs/project/loop/` | E S V | |
| `state-script` | `harness.py rules write`, `audit --write/--apply`, `graph --write`, `state loop`/`migrate` | E S V | |
| `delivery` | `harness.py deliver` / `delivery.py` | E S V | |
| `loop-run` | `harness.py loop`/`check`/`serve`, `run_loop.py` | E S V | |
| `install` | `npm`/`pnpm`/`yarn`/`bun` `install`/`add`/`ci`/`update`, `dotnet restore`/`add package`, `pip install` | E | |
| `path-scope` | `Write`/`Edit` outside the feature's paths in `.specs/project/loop/json/SCOPE.json` | E | |
| `repo-only` | `Write`/`Edit` outside the repository (temp folders excepted) | E | |

**Hard** rules cannot be lifted by `PERMISSIONS.json`. Every block and every rule you lifted is logged in `.specs/project/loop/internal/guard.log`.

In the headless loop there is a second lock. `claude -p` runs with the `pushurl` of every remote set to an invalid URL (`GIT_CONFIG_*`), so a push fails even if a regex misses it.

## Fail-open, on purpose

If the guard itself breaks, it lets the call through (exit 0) so a long loop does not stall. This covers an unreadable payload or an exception inside a check, but it is never silent:

- it writes a `guard-failopen` line to `guard.log`;
- it sends a notice through your channels (`harness.py notify`), at most once an hour.

A `PERMISSIONS.json` that is not valid JSON is ignored: nothing is lifted, and your project rules are not applied either. An invalid regex in `deny_*_regex` is skipped and reported on stderr (`invalid project regex ... skipped`); every other rule, hard rules included, keeps blocking. The broken rule itself does not block until you fix it. Always add rules through `harness.py rules propose`, which rejects invalid, empty and too-wide patterns. `harness.py check` re-runs `rules test` before each loop.

## Your rules: `.specs/project/loop/json/PERMISSIONS.json`

The guard finds `.specs/` by walking up from the payload's `cwd` (up to 8 levels). It reads `project/loop/json/PERMISSIONS.json`; a project on the old flat layout still works from `project/PERMISSIONS.json`. Shape:

```json
{
  "deny_bash_regex": ["<regex matched against the whole Bash/PowerShell command>"],
  "deny_path_regex": ["<regex matched against the path of Write/Edit/NotebookEdit and of shell writes>"],
  "allow": [{"rule": "<built-in rule id>", "quote": "<the user's exact words>", "at": "<date>"}]
}
```

- Regexes use Python `re` with `re.IGNORECASE`. In JSON, every backslash is doubled. Write `[\\\\/]` in JSON (`[\\/]` in the regex) to accept both `/` and `\` in paths.
- `deny_bash_regex` sees the whole command text, quotes included. `echo "never run terraform apply"` matches a `terraform apply` rule.
- `deny_path_regex` sees the path written by `Write`/`Edit`/`NotebookEdit` (usually absolute) and the targets of `>`, `tee`, `sed -i`, `Set-Content`... It does not see `cp`, `mv` or a script that writes files. Add a `deny_bash_regex` when that matters.
- SQL sent through a database MCP tool is not a shell command. It is covered by the built-in `dev-write`/`prod-write`, not by `deny_bash_regex`.
- `allow` lifts a built-in rule for this project. The guard only reads `rule` (a plain string also works). `quote` and `at` are the audit trail: write one only with the user's explicit words. Lifting `project-deny` switches off **all** your project rules.

## Cookbook (every example is tested)

This is the full `PERMISSIONS.json` the examples below use. `test_guard_cookbook.py` writes exactly this file into a temporary project and runs each must-block and must-pass case through the real `guard.py`. A must-block case must exit 2 with `project-deny` as the reason. A must-pass case must exit 0. It also fails if this README stops showing any of these regexes.

```json
{
  "deny_bash_regex": [
    "\\b(?:DROP\\s+(?:TABLE|DATABASE|SCHEMA)\\b|TRUNCATE\\s+(?:TABLE\\s+)?[\\w.`\"\\[])",
    "\\bDELETE\\s+FROM\\s+[\\w.`\"\\[\\]]+(?![^;\\n]*\\bWHERE\\b)",
    "\\bgit\\b[^\\n;&|]*?\\s(?:checkout|switch)\\s+(?:-[fqmB]\\s+|--\\S+\\s+)*(?:main|master)(?![\\w./-])",
    "\\bterraform\\s+(?:-\\S+\\s+)*(?:apply|destroy)\\b",
    "\\bkubectl\\b[^\\n;&|]*\\sdelete\\b"
  ],
  "deny_path_regex": [
    "(?:^|[\\\\/])migrations[\\\\/]",
    "(?:^|[\\\\/])infra[\\\\/]prod[\\\\/]"
  ],
  "allow": [
    {
      "rule": "install",
      "quote": "yes, you may run npm install in this project",
      "at": "2026-10-15"
    }
  ]
}
```

### 1. Destructive SQL: `DROP TABLE`, `TRUNCATE`, `DELETE FROM` without `WHERE`

```json
"\\b(?:DROP\\s+(?:TABLE|DATABASE|SCHEMA)\\b|TRUNCATE\\s+(?:TABLE\\s+)?[\\w.`\"\\[])",
"\\bDELETE\\s+FROM\\s+[\\w.`\"\\[\\]]+(?![^;\\n]*\\bWHERE\\b)"
```

| Must block | Must pass |
|---|---|
| `psql -c "DROP TABLE users"` | `psql -c "DELETE FROM users WHERE id = 42"` |
| `mysql -e 'truncate table orders'` | `truncate -s 0 app.log` |
| `sqlite3 app.db "TRUNCATE orders"` | `psql -c "SELECT * FROM dropped_tables"` |
| `psql -c "drop database shop"` | `grep -rn "DELETE FROM" src/` |
| `psql -c "DELETE FROM users"` | |
| `mysql -e 'delete from orders;'` | |
| `psql -c "DELETE FROM users; SELECT 1 WHERE true"` (the `WHERE` belongs to another statement) | |

### 2. Never work on `main`

Pushing to `main` (or `master`, or your integration branch) is already blocked by the hard built-in rule `push-protected`, in every profile, with no project rule. A regex cannot see which branch is checked out, so it cannot block "a commit while on `main`" directly. This rule blocks getting onto `main` in the first place. The loop itself always works on its own `loop/<date>` branch in a separate worktree.

```json
"\\bgit\\b[^\\n;&|]*?\\s(?:checkout|switch)\\s+(?:-[fqmB]\\s+|--\\S+\\s+)*(?:main|master)(?![\\w./-])"
```

| Must block | Must pass |
|---|---|
| `git checkout main` | `git checkout -b feature/main-menu` |
| `git switch master` | `git switch -c fix/login main` (new branch from `main`) |
| `git -C repo checkout -B main origin/main` | `git diff main...HEAD` |
| `git checkout main && git commit -m hotfix` | `git log --oneline main` |
| | `git commit -m 'feat: menu'` |

Proven in the `orchestrator` profile. Subagents cannot run write git at all (built-in `git-write`).

### 3. Infrastructure: `terraform apply`, `kubectl delete`

```json
"\\bterraform\\s+(?:-\\S+\\s+)*(?:apply|destroy)\\b",
"\\bkubectl\\b[^\\n;&|]*\\sdelete\\b"
```

| Must block | Must pass |
|---|---|
| `terraform apply -auto-approve` | `terraform plan -out tf.plan` |
| `terraform -chdir=infra destroy` | `terraform validate` |
| `kubectl delete pod api-0` | `kubectl get pods` |
| `kubectl -n prod delete deployment api` | `kubectl describe pod api-0` |

### 4. Protected folders: `migrations/`, `infra/prod/`

```json
"(?:^|[\\\\/])migrations[\\\\/]",
"(?:^|[\\\\/])infra[\\\\/]prod[\\\\/]"
```

| Must block | Must pass |
|---|---|
| Edit `db/migrations/0001_init.sql` | Edit `src/migrations_helper.py` |
| Edit `migrations/0002_users.py` | Edit `docs/migrations.md` |
| Edit `infra/prod/main.tf` | Edit `infra/staging/main.tf` |
| `echo "ALTER TABLE x" > db/migrations/0003.sql` | Edit `infra/production-notes.md` |
| `sed -i s/a/b/ infra/prod/values.yaml` | |

Reading these folders is still allowed. Only writes are blocked.

### 5. Lifting a built-in rule with the user's words

```json
"allow": [{"rule": "install", "quote": "yes, you may run npm install in this project", "at": "2026-10-15"}]
```

With this entry, `npm install left-pad` passes for an executor. Without it, it is blocked by `install`. `allow` never lifts a hard rule: `git push --force origin feature/x` stays blocked even with `"allow": ["push-force", "git-push"]`.

## The `orchestrator-create-block-rule` flow

You rarely edit `PERMISSIONS.json` by hand. Say "never ..." to the orchestrator, and the `orchestrator-create-block-rule` skill turns it into a rule with proof:

1. **Propose and test, writing nothing.** Each example runs through the real guard in the executor profile:
   ```
   python app/harness.py rules propose <repo> --id R-01 --phrase "never edit migrations" \
     --path '(?:^|[\\/])migrations[\\/]' \
     --block path:db/migrations/0001_init.sql --block path:migrations/0002_users.py \
     --pass path:src/migrations_helper.py --pass path:docs/migrations.md
   ```
   Use `--bash REGEX` with `--block "bash:<command>"` for a command rule. The script rejects an invalid, empty or too-wide regex. Because examples run as an executor, give a git rule pass examples that are read-only git (`git diff main...HEAD`, `git log main`). A write-git example is blocked there by `git-write` anyway.
2. **You approve** what was blocked and what passed.
3. **Write.** Run the same arguments with `write`, plus `--rule "<short text>" --reason "<why>"`. It only writes if the new cases pass and no older rule breaks. It updates `PERMISSIONS.json`, `.specs/project/loop/json/rules.tests.json` (the examples) and a row in `.specs/project/RULES.md`. Only the orchestrator may run `rules write` (`state-script` blocks subagents).
4. **Keep it proven.** `python app/harness.py rules test <repo>` re-runs every stored example, and `harness.py check` does it before each loop. `python app/harness.py rules list <repo>` lists the rules.

## Running the tests

From the repository root:

```
python -m unittest discover -s plugin/skills/orchestrator/hooks -p "test_guard*.py" -v
```

- `test_guard.py`: the built-in rules, profiles, layout fallbacks and fail-open.
- `test_guard_cookbook.py`: every example on this page.

Both run the real `guard.py` in temporary folders, with `LOOP_DATA_DIR` pointing at an empty folder, so no app, notice or network is touched.

A quick manual check that the guard answers:

```
echo '{"tool_name":"Bash","tool_input":{"command":"git push"}}' | python plugin/skills/orchestrator/hooks/guard.py --profile executor; echo $?
```

It must print `2`. `python app/harness.py doctor` runs the same check.
