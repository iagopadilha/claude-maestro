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
| `secrets` | reading, writing or running anything on `.secrets/`, the plugin's `shared/.env`, `~/.loop-harness/.env` or `app.json`, SSH private keys, or a config file copied into a worktree (`copied` in `SCOPE.json`, from REPOS.json `copy`), by name or by a recursive/ignore-bypassing search that would reach it (see "Copied config files") | O E S V | |
| `project-deny` | **your** `deny_bash_regex` and `deny_path_regex` (see the cookbook) | O E S V | |
| `git-add-all` | `git add .`, `git add -A`/`--all`, `git commit -a` | O | |
| `git-write` | any git subcommand except `status`, `diff`, `log`, `show`, `rev-parse`, `ls-files`, `blame`, `describe`, `grep`, `shortlog` | E S V | |
| `dev-write` | write SQL through a non-production database MCP tool | E S V | |
| `state-write` | writing `ROADMAP.md`, `FLOWS.md`, `STATE.md`, `RULES.md`, `phases/`, `archive/` or anything under `.specs/project/loop/` | E S V | |
| `state-script` | `harness.py rules write`, `audit --write/--apply`, `graph --write`, `state loop`/`migrate` | E S V | |
| `delivery` | `harness.py deliver` / `delivery.py` | E S V | |
| `loop-run` | `harness.py loop`/`check`/`serve`, `run_loop.py` | E S V | |
| `install` | `npm`/`pnpm`/`yarn`/`bun` `install`/`add`/`ci`/`update`, `dotnet restore`/`add package`, `pip install` | E | |
| `path-scope` | `Write`/`Edit` (and a shell redirect, `tee`, `Set-Content`, `Out-File`, `sed -i`) outside the feature's paths in `.specs/project/loop/json/SCOPE.json` (absolute entries, or relative to the primary root) | E | |
| `grafana-write` | any `mcp__grafana__*` tool that is not a read: `create_*`, `update_*`, `delete_*`, `add_*`, `install_*`, `alerting_manage_*`, `create_snapshot`/`delete_snapshot`, and `grafana_api_request` with a method other than GET (reads such as `query_loki_*`, `list_*`, `get_*`, `search_*`, `find_*`, `check_*`, `generate_deeplink` stay allowed). No `PERMISSIONS.json` entry releases it | every profile (O E S V and planning) | **yes** |
| `repo-only` | `Write`/`Edit` (and a shell write) outside the project's worktrees (the primary root plus `roots` of `SCOPE.json`; temp folders excepted), and a git write whose `-C`/cwd is outside them | O E | |

**Hard** rules cannot be lifted by `PERMISSIONS.json`. Every block and every rule you lifted is logged in `.specs/project/loop/internal/guard.log`.

In the headless loop there is a second lock. `claude -p` runs with the `pushurl` of every remote set to an invalid URL (`GIT_CONFIG_*`), so a push fails even if a regex misses it.

## Copied config files: what the `secrets` rule covers and what it does not

A config file copied into a worktree (`copied` in `SCOPE.json`, e.g. `.env.local`) is git-ignored, and the guard keeps agents from reading it. Checked with ripgrep 14.1.1 in a throwaway git repo whose `.gitignore` lists `.env.local`: `rg SECRET` (what the Grep tool runs) **skips** it, and so does `rg --hidden`; `rg --no-ignore` alone still skips it (it is also a hidden file, and that flag does not unhide), but `rg --no-ignore --hidden` and `rg -uu` **read it**; `rg -g '.env*'` **reads it** (a glob whitelists an ignored file, and the Grep tool's `glob` parameter does the same); `grep -r` reads it; the Glob tool **lists** it (names only, it does not honour `.gitignore`). So what is blocked, with the `secrets` rule, when the target folder is or contains the folder of a copied file:

- by name: any tool call or command that names the file (full path in any spelling that normalizes to it, or its path inside the worktree when the cwd is that worktree);
- Bash/PowerShell: `rg`/`ag`/`ack` with `--no-ignore*`, `-u`/`-uu`, `--unrestricted`, or a `-g/--glob` that singles the file out; `grep -r/-R/--recursive` (unless `--include` excludes it or `--exclude` names it); `git grep --no-index`/`--no-exclude-standard`; `find` feeding `-exec`/`xargs` to a reader (unless every `-name` excludes it); `Get-ChildItem -Recurse` (or `gci -r`, `ls -R`) piped to `Select-String`/`Get-Content`/`cat`/`type`...; `findstr /s`; recursive copy or archive (`cp -r`, `Copy-Item -Recurse`, `xcopy /s`, `robocopy`, `rsync`, `zip`, `tar -c`), which would move the secret to a name the guard does not know; a glob that singles the name out (`cat .env*`, `type *.local`) and, for `Select-String`/`Get-Content`/`findstr`, a bare `*` in that folder;
- Grep tool: a `glob` that singles the file out, or an ignore-bypass parameter; Glob tool: a pattern that singles the file out (`.env*`, `*.local`; a bare `*` or `**/*.ts` is fine).

A normal source search keeps working: `Grep` without a glob, `rg pattern`, `grep -r pattern src/`, `find . -name '*.ts' -exec grep ...`, builds and tests (a build that loads the file is not a tool call on it).

Residual risk, on purpose and not closed: the check is text analysis, not a sandbox. A target that comes from a variable or sub-shell (`$(...)`, `$env:X`), a script or program that opens the file itself (`python -c`, `node -e`, a test that prints `process.env`), a wildcard such as `cat *` in bash (the shell does not expand it to dotfiles, so it is not flagged), commands this list does not know, and any path that reaches the file through a symlink or junction are not seen. The file is also readable by the build and test tools the executor is allowed to run. The real protection is that these files hold development values only: never copy a production secret into a worktree.

## Locating `.specs/`, path normalization and the temp exemption

- **`LOOP_SPECS_DIR`.** `SCOPE.json`, `PERMISSIONS.json` and `guard.log` live only in the PRIMARY worktree's `.specs/`. A hook whose cwd is another worktree (after a `cd`, or with `--add-dir`) finds no `.specs/` walking up, which would turn path-scope, repo-only and the copied-secrets rules off. The engine therefore exports `LOOP_SPECS_DIR` (absolute path of the primary `.specs` folder) in the `claude -p` environment and the hooks inherit it. When set, it wins over the walk-up; it is honoured only when it is absolute, an existing folder and named `.specs` (anything else is ignored). Without it the old walk-up from the payload `cwd` is unchanged (the app's planning/map jobs rely on that). The project root is always `dirname(specs_dir)`; a relative tool target (`apps/z/a.ts`) is resolved against the hook's cwd, never against the specs root, and then judged against the roots and PATHS.
- **Normalized comparison of copied secrets.** Every path-looking token of the command or tool input is normalized before it is compared with the `copied` absolute paths: backslashes and `//` collapsed, `.` and `..` resolved, a relative path resolved against the hook cwd, git-bash `/c/...` and WSL `/mnt/c/...` turned into `c:/...` (Windows), case-insensitive. A token that lands inside any listed worktree root with the same tail as a copied file (`.env.local` in the primary root, for instance) counts too. So `R://wt/.env.local`, `R:/wt/./.env.local`, `R:/wt/apps/../.env.local`, `../wt/.env.local` and `/r/wt/.env.local` are all blocked. The old substring match stays as a second net.
- **Temp exemption.** Writes to a temp folder (a `tmp`, `temp` or `scratchpad` path segment) outside the project's roots are allowed, except when any ancestor folder of the target holds a `.git` entry (folder or file, so linked worktrees count): a path inside a git working tree is code and is never exempt, wherever that repo lives. Genuine temp files (no `.git` above) keep the exemption.
- **Case.** Paths are lowercased for comparison only where the filesystem is case-insensitive (Windows, macOS: the `CASE_INSENSITIVE` flag of `guard.py`). On a case-sensitive filesystem the original case is kept end to end, so the `.git` ancestor check (an `os.path.exists` on the real path, resolved against the hook cwd) and every other filesystem lookup hit the real file; glob/name matching and the build-output folder names are folded the same way.

## Shell writes are confined like Write/Edit

`>`, `>>`, `tee`, `Set-Content`/`Add-Content`/`Out-File` (also with `-Path`/`-FilePath`/`-LiteralPath`) and `sed -i` get the same confinement as `Write`/`Edit`, per statement (a `cd` before them is followed): an executor (`path-scope`) writes only inside the feature's PATHS, the orchestrator and the executor (`repo-only`) only inside the worktrees, spec and planning (`specs-only`) only inside `.specs/` (and `docs/codebase/` for spec). Exempt: temp folders, `/dev/*`, `nul`, `$null`, and build output folders inside a worktree (`bin`, `obj`, `node_modules`, `.pytest_cache`, `__pycache__`, `dist`, `coverage`, `.next`, `.venv`...). The tools' own writes (`dotnet build` filling `bin/obj`, `pytest` filling `.pytest_cache`) never go through the shell, so they are not seen at all. A `>` inside a quoted string (`python -c "print('a > b')"`) is text, not a redirect. Not covered: a target built from a variable (`$env:X\out`, `$(...)`), and writers other than the ones listed (`cp`, `mv`, `Copy-Item`, `New-Item`, a script that opens the file itself).

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
