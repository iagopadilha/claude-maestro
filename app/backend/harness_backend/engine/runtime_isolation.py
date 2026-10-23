#!/usr/bin/env python3
"""Runtime isolation of every `claude -p` the app starts (standard library only; imported by the engine as a script sibling and by
`infrastructure/claude_client.py` as `harness_backend.engine.runtime_isolation`).

Why: a plain `claude -p` loads the user's settings: ~100 skills, every enabled plugin, the user's hooks and the plugin MCP servers (even
with `--strict-mcp-config`). Isolation starts the process with `--setting-sources project,local` (the project's own CLAUDE.md and
settings still load) plus `--add-dir=<bundle>`, a folder `<data dir>/runtime/bundle/.claude/{skills,agents}` that holds exactly the
plugin agents and the allowlisted skills under their plain names (`--plugin-dir` would namespace them as `<plugin>:<name>` and break
dispatch by name). The guard hook, which used to come from the user's `~/.claude/settings.json`, goes through inline `--settings`.

Kill switch: `"loop_isolation": false` in `<LOOP_DATA_DIR or ~/.loop-harness>/config.json` (default true) gives back today's argv.

Allowlist keys (one per profile name the guard can resolve; `LOOP_SKILLS_FILE` is `{profile: [skill, ...]}`):
  orchestrator, planning, roadmapmap, subprojects, systemmap, spec, executor, verifier   (the keys of guard.py `PROFILES`)
  auto = orchestrator   (inline hook `--profile auto` of the loop turns and of app jobs with no `hook_profile`; the guard resolves it to
                         the agent's profile or, for the main session, to `orchestrator`, so the key is only a safety net)
App job `hook_profile` values found in services/: planning (planning chat, roadmap, follow-up), roadmapmap (roadmap with
`--map-first`), spec (map jobs and subproject maps), subprojects, systemmap. Chat-like jobs with no hook profile (read-only chat,
quarantine summary) run under `auto`. Follow-up reuses `planning` (its service says so), so it maps to the planning list.

Environment: `LOOP_CLAUDE_HOME` (default `~/.claude`) names the user's Claude folder (skills, `settings.json`); tests point it at a temp dir.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time

DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
DEFAULT_DATA_DIR_NAME = ".loop-harness"
CLAUDE_HOME_VARIABLE = "LOOP_CLAUDE_HOME"
CONFIG_KEY = "loop_isolation"
EXTRA_SKILLS_KEY = "loop_extra_skills"
CAVEMAN_KEY = "loop_caveman"
CAVEMAN_MARKER = "caveman"
SESSION_START_EVENT = "SessionStart"
RUNTIME_DIR_NAME = "runtime"
BUNDLE_DIR_NAME = "bundle"
SIGNATURE_FILE_NAME = ".signature.json"
ALLOWLIST_FILE_NAME = "skills-allowlist.json"
MEMO_SECONDS = 30
SUBPROJECTS_RELATIVE_PATH = os.path.join(".specs", "project", "SUBPROJECTS.json")

PLANNING_SKILLS = ["orchestrator", "tlc-spec-driven", "repo-catalog", "backend-dev", "backend-dev-skill", "frontend-dev", "frontend-dev-skill"]
DEFAULT_ALLOWLIST = {
    "orchestrator": ["orchestrator", "orchestrator-audit-dependencies", "orchestrator-create-block-rule", "orchestrator-show-progress-graph",
                     "tlc-spec-driven", "repo-catalog"],
    "planning": PLANNING_SKILLS, "roadmapmap": PLANNING_SKILLS, "subprojects": PLANNING_SKILLS, "systemmap": PLANNING_SKILLS,
    "spec": ["tlc-spec-driven", "repo-catalog", "backend-dev", "backend-dev-skill", "frontend-dev", "frontend-dev-skill", "investigate-first"],
    "executor": ["backend-dev", "backend-dev-skill", "frontend-dev", "frontend-dev-skill", "safe-refactor", "refactoring-guru-techniques",
                 "migration", "surgical-patch", "playwright-cli"],
    "verifier": ["flow-validator", "playwright-cli", "playwright-trace", "playwright-component-testing", "verify-and-stop"],
}
SUBPROJECT_SKILL_PROFILES = ("spec", "executor")  # profiles that also get the skills named in SUBPROJECTS.json
AUTO_ALIAS = ("auto", "orchestrator")

_memo = {}
_memo_lock = threading.Lock()
_build_lock = threading.Lock()


def data_dir():
    return os.path.abspath(os.environ.get(DATA_DIR_VARIABLE) or os.path.join(os.path.expanduser("~"), DEFAULT_DATA_DIR_NAME))


def claude_home():
    return os.path.abspath(os.environ.get(CLAUDE_HOME_VARIABLE) or os.path.join(os.path.expanduser("~"), ".claude"))


def user_skills_dir():
    return os.path.join(claude_home(), "skills")


def read_config():
    try:
        with open(os.path.join(data_dir(), "config.json"), encoding="utf-8") as handle:
            content = json.load(handle)
        return content if isinstance(content, dict) else {}
    except (OSError, ValueError):
        return {}


def isolation_enabled(config=None):
    """`loop_isolation` of config.json; only an explicit false (boolean) turns it off."""
    config = read_config() if config is None else config
    return config.get(CONFIG_KEY, True) is not False


def project_skill_names(project_root):
    """Skills named in `<root>/.specs/project/SUBPROJECTS.json` (any `skill` of a project entry, `-` and skipped entries ignored)."""
    if not project_root:
        return []
    try:
        with open(os.path.join(project_root, SUBPROJECTS_RELATIVE_PATH), encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    lists = []
    if isinstance(data, dict):
        for key in ("subprojects", "projects"):
            lists.append(data.get(key))
        repos = data.get("repos")
        for entry in (repos.values() if isinstance(repos, dict) else []):
            lists.append(entry.get("projects") if isinstance(entry, dict) else None)
    names = []
    for items in lists:
        for item in items if isinstance(items, list) else []:
            skill = str(item.get("skill") or "").strip() if isinstance(item, dict) and not item.get("skip") else ""
            if skill and skill != "-" and skill not in names:
                names.append(skill)
    return names


def skills_allowlist(project_root=None, config=None):
    """{profile: [skill, ...]} (spec ACC-04): the defaults, plus the SUBPROJECTS.json skills for spec and executor, plus `loop_extra_skills`
    of config.json for every profile. `auto` mirrors `orchestrator`."""
    config = read_config() if config is None else config
    allowlist = {profile: list(skills) for profile, skills in DEFAULT_ALLOWLIST.items()}
    for profile in SUBPROJECT_SKILL_PROFILES:
        allowlist[profile] += [name for name in project_skill_names(project_root) if name not in allowlist[profile]]
    extra = config.get(EXTRA_SKILLS_KEY)
    names = [str(item).strip() for item in extra if str(item).strip()] if isinstance(extra, list) else []
    if config.get(CAVEMAN_KEY, False) is True:
        names.append(CAVEMAN_MARKER)  # the carried hook tells the model to load the `caveman` skill: allow it (and bundle it) everywhere
    for name in names:
        for skills in allowlist.values():
            if name not in skills:
                skills.append(name)
    allowlist["auto"] = list(allowlist["orchestrator"])
    return allowlist


def all_skill_names(allowlist):
    names = []
    for skills in allowlist.values():
        names += [name for name in skills if name not in names]
    return sorted(names)


def plugin_root():
    """Plugin root that holds the orchestrator skill ('' when it is not installed)."""
    try:
        import app_paths  # the engine's path bridge (backend on sys.path)
        skill_dir = app_paths.locate_skill_dir()
    except ImportError:
        from harness_backend.config import skill_locator
        skill_dir = skill_locator.locate_orchestrator_skill_dir()
    return os.path.abspath(os.path.join(skill_dir, "..", "..")) if skill_dir else ""


# ---- bundle ------------------------------------------------------------------------------------------------------------------------

def tree_stamp(path):
    """(newest mtime in ns, total size in bytes) of a file or folder tree."""
    newest, total = 0, 0
    for folder, _, files in os.walk(path) if os.path.isdir(path) else [(os.path.dirname(path), [], [os.path.basename(path)])]:
        for name in files:
            try:
                info = os.stat(os.path.join(folder, name))
            except OSError:
                continue
            newest, total = max(newest, info.st_mtime_ns), total + info.st_size
    return newest, total


def resolve_skill_sources(plugin_dir, user_dir, names):
    """({name: source folder}, [missing names]): `<user skills>/<name>` first, else `<plugin>/skills/<name>`; a folder needs a SKILL.md."""
    found, skipped = {}, []
    for name in names:
        for base in (user_dir, os.path.join(plugin_dir, "skills") if plugin_dir else ""):
            candidate = os.path.join(base, name) if base else ""
            if candidate and os.path.isfile(os.path.join(candidate, "SKILL.md")):
                found[name] = candidate
                break
        else:
            skipped.append(name)
    return found, skipped


def list_agent_files(plugin_dir):
    folder = os.path.join(plugin_dir, "agents") if plugin_dir else ""
    return sorted(name for name in os.listdir(folder) if name.endswith(".md")) if folder and os.path.isdir(folder) else []


def compute_signature(skill_sources, plugin_dir, agent_files):
    entries = [["skill", name, source] + list(tree_stamp(source)) for name, source in sorted(skill_sources.items())]
    entries += [["agent", name, os.path.join(plugin_dir, "agents", name)] + list(tree_stamp(os.path.join(plugin_dir, "agents", name)))
                for name in agent_files]
    return entries


def read_signature(bundle):
    try:
        with open(os.path.join(bundle, SIGNATURE_FILE_NAME), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def build_bundle_folder(target, skill_sources, plugin_dir, agent_files, signature):
    skills_dir = os.path.join(target, ".claude", "skills")
    agents_dir = os.path.join(target, ".claude", "agents")
    os.makedirs(skills_dir)
    os.makedirs(agents_dir)
    for name, source in skill_sources.items():
        shutil.copytree(source, os.path.join(skills_dir, name))
    for name in agent_files:
        shutil.copyfile(os.path.join(plugin_dir, "agents", name), os.path.join(agents_dir, name))
    with open(os.path.join(target, SIGNATURE_FILE_NAME), "w", encoding="utf-8") as handle:
        json.dump(signature, handle)


def swap_in(temp, bundle):
    """Puts `temp` at `bundle`: the old folder is renamed aside first (a folder cannot be replaced over a non-empty one), then removed."""
    aside = ""
    if os.path.exists(bundle):
        aside = "%s.old-%d-%s" % (bundle, os.getpid(), hashlib.md5(str(time.time()).encode()).hexdigest()[:6])
        os.replace(bundle, aside)
    try:
        os.replace(temp, bundle)
    except OSError:
        if aside and not os.path.exists(bundle):
            os.replace(aside, bundle)
        raise
    if aside:
        shutil.rmtree(aside, ignore_errors=True)


def ensure_bundle(data_dir_path, plugin_root_path, user_skills_path, names, use_memo=True):
    """Builds `<data dir>/runtime/bundle` when the source signature changed; cheap when it did not (and a 30 s in-process memo makes a call
    per command nearly free). Returns {"path", "skills", "skipped", "agents", "rebuilt"} (+ "error" when the swap failed and the old bundle
    stays). The build happens in a temp folder beside it and is swapped in, so a concurrent run never sees a half bundle."""
    names = sorted(set(names))
    key = (os.path.abspath(data_dir_path), plugin_root_path or "", user_skills_path or "", tuple(names))
    now = time.time()
    with _memo_lock:
        hit = _memo.get(key)
    if use_memo and hit and now - hit[0] < MEMO_SECONDS and os.path.isdir(hit[1]["path"]):
        return dict(hit[1], rebuilt=False)
    runtime = os.path.join(os.path.abspath(data_dir_path), RUNTIME_DIR_NAME)
    bundle = os.path.join(runtime, BUNDLE_DIR_NAME)
    sources, skipped = resolve_skill_sources(plugin_root_path, user_skills_path, names)
    agent_files = list_agent_files(plugin_root_path)
    signature = compute_signature(sources, plugin_root_path, agent_files)
    result = {"path": bundle, "skills": sorted(sources), "skipped": skipped, "agents": [name[:-3] for name in agent_files], "rebuilt": False}
    with _build_lock:
        stored = read_signature(bundle)
        if stored != signature:
            os.makedirs(runtime, exist_ok=True)
            temp = tempfile.mkdtemp(prefix="bundle.tmp-", dir=runtime)
            try:
                os.rmdir(temp)  # the build creates the folder itself (copytree/makedirs need it absent)
                build_bundle_folder(temp, sources, plugin_root_path, agent_files, signature)
                swap_in(temp, bundle)
                result["rebuilt"] = True
            except OSError as error:
                shutil.rmtree(temp, ignore_errors=True)
                result["error"] = "bundle not rebuilt: %s" % error
    with _memo_lock:
        _memo[key] = (now, dict(result))
    return result


def clear_memo():
    with _memo_lock:
        _memo.clear()


def write_allowlist_file(data_dir_path, allowlist):
    """Writes the allowlist JSON (atomically) and returns its path. The shared default file `skills-allowlist.json` holds the allowlist of
    a project with no extras; an allowlist that differs (SUBPROJECTS.json skills) goes to `skills-allowlist-<hash>.json`, so two projects
    running at once never overwrite each other's file."""
    runtime = os.path.join(os.path.abspath(data_dir_path), RUNTIME_DIR_NAME)
    os.makedirs(runtime, exist_ok=True)
    text = json.dumps(allowlist, indent=2, sort_keys=True)
    path = os.path.join(runtime, ALLOWLIST_FILE_NAME)
    if allowlist != skills_allowlist(None, read_config()):
        path = os.path.join(runtime, "skills-allowlist-%s.json" % hashlib.md5(text.encode("utf-8")).hexdigest()[:10])
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return path
    except OSError:
        pass
    temp = "%s.tmp-%d" % (path, os.getpid())
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temp, path)
    return path


# ---- argv / env / settings -----------------------------------------------------------------------------------------------------------

def guard_command(guard_path, profile):
    """The PreToolUse hook command line (shared by the loop turns and by `ClaudeClient.build_hook_settings`)."""
    return 'python "%s" --profile %s' % (os.path.abspath(guard_path).replace("\\", "/"), profile)


def hook_block(command):
    return {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}


def read_user_settings():
    try:
        with open(os.path.join(claude_home(), "settings.json"), encoding="utf-8-sig") as handle:
            content = json.load(handle)
        return content if isinstance(content, dict) else {}
    except (OSError, ValueError):
        return {}


def find_caveman_hooks(user_settings):
    """The user's `SessionStart` hook groups reduced to the hooks whose command mentions `caveman` (case-insensitive); a group with no such
    hook is dropped and the other fields of a group (`matcher`) are kept. [] when the user has none (the option is then "not installed")."""
    events = user_settings.get("hooks") if isinstance(user_settings, dict) else None
    groups = events.get(SESSION_START_EVENT) if isinstance(events, dict) else None
    found = []
    for group in groups if isinstance(groups, list) else []:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        kept = [hook for hook in hooks if isinstance(hook, dict) and CAVEMAN_MARKER in str(hook.get("command") or "").lower()]             if isinstance(hooks, list) else []
        if kept:
            found.append(dict(group, hooks=kept))
    return found


def caveman_hooks_to_carry(config=None, user_settings=None):
    """The caveman hook groups a `claude -p` carries: only with isolation on AND `loop_caveman` true (boolean) in config.json, else []."""
    config = read_config() if config is None else config
    if not isolation_enabled(config) or config.get(CAVEMAN_KEY, False) is not True:
        return []
    return find_caveman_hooks(read_user_settings() if user_settings is None else user_settings)


def guard_settings(guard_path, profile, user_settings=None):
    """Inline `--settings` JSON: the guard as a PreToolUse hook (`--profile <profile>`) plus the user's `autoMode` block copied from
    `~/.claude/settings.json` (or from `user_settings` when given) so `--permission-mode auto` keeps its classifier context.
    '' when there is neither a guard nor an `autoMode`. The user's `permissions` are not carried (the app passes `--permission-mode`).
    With `loop_caveman` on (config.json) the user's caveman `SessionStart` hooks join the same file (`caveman_hooks_to_carry`)."""
    user_settings = read_user_settings() if user_settings is None else user_settings
    settings = {}
    if guard_path:
        settings["hooks"] = hook_block(guard_command(guard_path, profile))
    caveman_hooks = caveman_hooks_to_carry(None, user_settings)
    if caveman_hooks:
        settings.setdefault("hooks", {})[SESSION_START_EVENT] = caveman_hooks
    if isinstance(user_settings, dict) and "autoMode" in user_settings:
        settings["autoMode"] = user_settings["autoMode"]
    return json.dumps(settings) if settings else ""


def write_settings_file(runtime_dir, settings_json):
    """Writes the settings JSON next to the bundle (name by content hash, atomic) and returns its path. A file, never the inline JSON: a
    batch fallback binary (`cc.cmd`) goes through cmd.exe, which splits an argument on `&`, `|`, `<`, `>` (the copied `autoMode` text has them)."""
    os.makedirs(runtime_dir, exist_ok=True)
    path = os.path.join(runtime_dir, "settings-%s.json" % hashlib.md5(settings_json.encode("utf-8")).hexdigest()[:10])
    if not os.path.isfile(path):
        temp = "%s.tmp-%d" % (path, os.getpid())
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(settings_json)
        os.replace(temp, path)
    return path


def isolation_argv(bundle_path, settings_json):
    argv = ["--setting-sources", "project,local", "--add-dir=%s" % bundle_path]
    if settings_json:
        argv += ["--settings", write_settings_file(os.path.join(data_dir(), RUNTIME_DIR_NAME), settings_json)]
    return argv


def isolation_env(allowlist_file):
    return {"LOOP_ISOLATED": "1", "LOOP_SKILLS_FILE": allowlist_file}


def prepare(project_root=None, config=None, plugin_root_path=None):
    """Everything a `claude -p` needs when isolation is on: {"bundle": ensure_bundle result, "allowlist_file": path}. Raises nothing on a
    missing plugin (the bundle then holds no agents and the report lists the skipped skills)."""
    config = read_config() if config is None else config
    allowlist = skills_allowlist(project_root, config)
    root = plugin_root() if plugin_root_path is None else plugin_root_path
    bundle = ensure_bundle(data_dir(), root, user_skills_dir(), all_skill_names(allowlist))
    return {"bundle": bundle, "allowlist_file": write_allowlist_file(data_dir(), allowlist)}


def project_root_of(folder):
    """The folder (walking up from `folder`) that holds `.specs`; `folder` itself when none does."""
    here = os.path.abspath(folder or os.getcwd())
    for _ in range(8):
        if os.path.isdir(os.path.join(here, ".specs")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return os.path.abspath(folder or os.getcwd())


if __name__ == "__main__":
    print(json.dumps(prepare(sys.argv[1] if len(sys.argv) > 1 else None), indent=2))
