#!/usr/bin/env python3
"""`harness.py doctor --access`: what a `claude -p` of this app can actually reach (standard library only; a report, never a gate).

Three parts:
  static  MCP servers present (mcp_access), MCP tools each guard profile allows (`MCP_ALLOW`, loaded from the plugin's guard.py, not
          copied), the skills allowlist of the project (the skills bound to the Standards slots are labelled `(slot: <name>)`), the
          dev toolkit root, the bundle (agents, skills, skipped skills) and whether isolation is on.
  probe   the real isolated argv with a throw-away prompt; stdout is read until the `system/init` event (which lists tools, MCP servers,
          agents, skills and plugins the process loaded) and the process is killed. Anything outside the allowlists is flagged.
          An auth refusal still prints the init first, so the probe costs nothing in that case.
  guard   summary of the last lines of the project's `.specs/project/loop/internal/guard.log`.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import claude_binaries  # noqa: E402
import mcp_access  # noqa: E402
import runtime_isolation  # noqa: E402
import standards  # noqa: E402

PROBE_PROMPT = "reply only: ok"
PROBE_MODEL = "haiku"
PROBE_BUDGET_USD = "0.05"
PROBE_TIMEOUT_SECONDS = 60
PROBE_PROFILE = "auto"
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
GUARD_LOG_RELATIVE = os.path.join(".specs", "project", "loop", "internal", "guard.log")
GUARD_LOG_LEGACY_RELATIVE = os.path.join(".specs", "project", "guard.log")
GUARD_LOG_LINES = 200
DENIAL_DECISIONS = ("blocked", "deny")
LAST_DENIALS = 10
# What Claude Code itself ships (never flagged): the agents/skills below are built into the CLI, the plugin name `builtin` is its own.
BUILTIN_AGENTS = {"general-purpose", "explore", "plan", "statusline-setup", "claude-code-guide", "bash", "claude"}
BUILTIN_SKILLS = {"update-config", "keybindings-help", "code-review", "simplify", "fewer-permission-prompts", "loop", "schedule", "claude-api",
                  "workflow-authoring", "run", "plugin-authoring", "init", "security-review", "review", "debug", "batch", "verify", "compact",
                  "deep-research", "dataviz", "run-skill-generator",
                  "context", "cost", "help", "model", "clear", "usage", "config", "memory", "resume", "status", "doctor"}


# ---- static ------------------------------------------------------------------------------------------------------------------------

def guard_path(plugin_root_path):
    return os.path.join(plugin_root_path, "skills", "orchestrator", "hooks", "guard.py") if plugin_root_path else ""


def load_guard_mcp_allow(path):
    """{profile: sorted tool names} read from guard.py's `MCP_ALLOW` by loading the module from its path ({} when it cannot be loaded)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        spec = importlib.util.spec_from_file_location("access_report_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {profile: sorted(tools) for profile, tools in sorted(module.MCP_ALLOW.items())}
    except Exception:  # a broken guard must not break a report
        return {}


def slot_skill_labels(project_root):
    """{plain skill name: "slot1, slot2"} for the skills bound to the Standards slots of the project (root and subprojects)."""
    slots = {}
    for entry in standards.project_bindings(project_root):
        for slot, value in entry["bindings"].items():
            name = standards.skill_name(value)
            if name and slot not in slots.setdefault(name, []):
                slots[name].append(slot)
    return {name: ", ".join(names) for name, names in slots.items()}


def label_skill(name, labels):
    return "%s (slot: %s)" % (name, labels[name]) if name in labels else name


def build_static(project_root, plugin_root_path=None, config=None):
    config = runtime_isolation.read_config() if config is None else config
    root = runtime_isolation.plugin_root() if plugin_root_path is None else plugin_root_path
    enabled = runtime_isolation.isolation_enabled(config)
    access = mcp_access.build_access(runtime_isolation.data_dir())
    guard = guard_path(root)
    report = {
        "project": project_root, "plugin_root": root, "toolkit_root": runtime_isolation.toolkit_root(config),
        "guard": guard if os.path.isfile(guard) else "", "isolation": enabled,
        "slot_skills": slot_skill_labels(project_root),
        "mcp_servers": [{"server": row["server"], "tools": row["tools"]} for row in access["servers"]],
        "mcp_config_path": access["config_path"],
        "mcp_allow_by_profile": load_guard_mcp_allow(guard),
        "skills_allowlist": runtime_isolation.skills_allowlist(project_root, config),
        "bundle": None, "allowlist_file": "",
    }
    if enabled:
        prepared = runtime_isolation.prepare(project_root, config, root)
        bundle = prepared["bundle"]
        report["bundle"] = {key: bundle.get(key) for key in ("path", "skills", "skipped", "agents", "rebuilt", "error") if key in bundle}
        report["allowlist_file"] = prepared["allowlist_file"]
    return report


# ---- probe -------------------------------------------------------------------------------------------------------------------------

def build_probe_argv(static, config=None):
    """(argv, stdin_text, env) of the isolated probe, built like `ClaudeClient.build_command` does for a job with no hook profile."""
    config = runtime_isolation.read_config() if config is None else config
    argv = claude_binaries.base_argv(str(config.get("claude_binary") or claude_binaries.DEFAULT_BINARY_NAME))
    argv += ["-p", PROBE_PROMPT, "--model", PROBE_MODEL, "--max-budget-usd", PROBE_BUDGET_USD, "--output-format", "stream-json", "--verbose"]
    bundle = (static.get("bundle") or {}).get("path") or ""
    argv += runtime_isolation.isolation_argv(bundle, runtime_isolation.guard_settings(static.get("guard"), PROBE_PROFILE))
    argv += ["--strict-mcp-config", "--mcp-config", static.get("mcp_config_path") or EMPTY_MCP_CONFIG]
    argv, stdin_text = claude_binaries.prompt_through_stdin(argv, "")
    env = dict(os.environ, **runtime_isolation.isolation_env(static.get("allowlist_file") or ""))
    return argv, stdin_text, env


def read_until_init(argv, stdin_text, env, cwd, timeout=PROBE_TIMEOUT_SECONDS):
    """Starts the process, reads stdout lines until the `system/init` event, kills it. Returns (init event or None, error text)."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=cwd or None,
                                   text=True, encoding="utf-8", errors="replace")
    except OSError as error:
        return None, "could not start: %s" % error
    found = {}
    done = threading.Event()

    def reader():
        try:
            for line in process.stdout:
                event = parse_init_line(line)
                if event:
                    found["init"] = event
                    break
        except (OSError, ValueError):
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    try:
        if stdin_text:
            process.stdin.write(stdin_text)
        process.stdin.close()
    except OSError:
        pass
    finished = done.wait(timeout)
    try:
        process.kill()
        process.wait(5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.stdout.close()
    except (OSError, ValueError):
        pass
    if "init" in found:
        return found["init"], ""
    return None, ("timeout after %d s without an init event" % timeout) if not finished else "the process ended without an init event"


def parse_init_line(line):
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init" else None


def short_name(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("id") or ""
    return str(value or "").rsplit(":", 1)[-1].strip()


def is_builtin_plugin(entry):
    text = json.dumps(entry).lower() if isinstance(entry, (dict, list)) else str(entry).lower()
    return "builtin" in text or "built-in" in text


def analyze_init(init, static):
    """Summary of an init event plus the flags (anything outside the allowlists)."""
    tools = [str(item) for item in init.get("tools") or []]
    servers = [short_name(item) for item in init.get("mcp_servers") or []]
    agents = [str(item) for item in init.get("agents") or []]
    skills = [str(item) for item in init.get("skills") or []]
    plugins = list(init.get("plugins") or [])
    bundle = static.get("bundle") or {}
    allowed_servers = {row["server"] for row in static.get("mcp_servers") or []}
    allowed_skills = {name.lower() for names in (static.get("skills_allowlist") or {}).values() for name in names}
    bundle_agents = {name.lower() for name in bundle.get("agents") or []}
    flags = []
    for name in servers:
        if name not in allowed_servers:
            flags.append("mcp server outside the generated config: %s" % name)
    for name in agents:
        if short_name(name).lower() not in bundle_agents and short_name(name).lower() not in BUILTIN_AGENTS:
            flags.append("agent outside the bundle: %s" % name)
    for name in skills:
        plain = short_name(name).lower()
        if plain not in allowed_skills and plain not in BUILTIN_SKILLS:
            flags.append("skill outside the allowlist: %s" % name)
    for entry in plugins:
        if not is_builtin_plugin(entry):
            flags.append("plugin that is not builtin: %s" % short_name(entry))
    return {"model": init.get("model"), "tool_count": len(tools), "mcp_tools": sorted(item for item in tools if item.startswith("mcp__")),
            "mcp_servers": init.get("mcp_servers") or [], "agents": agents, "skills": skills,
            "plugins": [entry for entry in plugins if not is_builtin_plugin(entry)], "flags": flags}


def run_probe(static, config=None, project_root=None, timeout=PROBE_TIMEOUT_SECONDS):
    argv, stdin_text, env = build_probe_argv(static, config)
    init, error = read_until_init(argv, stdin_text, env, project_root, timeout)
    if not init:
        return {"ok": False, "error": error, "flags": []}
    return dict(analyze_init(init, static), ok=True, error="")


# ---- guard.log ---------------------------------------------------------------------------------------------------------------------

def read_guard_log(project_root, limit=GUARD_LOG_LINES):
    for relative in (GUARD_LOG_RELATIVE, GUARD_LOG_LEGACY_RELATIVE):
        path = os.path.join(project_root, relative)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().splitlines()[-limit:]
            except OSError:
                return path, []
            entries = []
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
            return path, entries
    return "", []


def summarize_guard_log(project_root, limit=GUARD_LOG_LINES):
    path, entries = read_guard_log(project_root, limit)
    counts = {"decision": {}, "rule": {}, "agent": {}}
    for entry in entries:
        for key, value in (("decision", entry.get("decision") or "-"), ("rule", entry.get("rule") or "-"), ("agent", entry.get("agent") or "-")):
            counts[key][value] = counts[key].get(value, 0) + 1
    denials = [{key: entry.get(key, "") for key in ("t", "profile", "rule", "agent", "detail")}
               for entry in entries if entry.get("decision") in DENIAL_DECISIONS][-LAST_DENIALS:]
    return {"path": path, "lines": len(entries), "by_decision": counts["decision"], "by_rule": counts["rule"], "by_agent": counts["agent"],
            "last_denials": denials}


# ---- report ------------------------------------------------------------------------------------------------------------------------

def build_report(project_root, probe=True, config=None, plugin_root_path=None, timeout=PROBE_TIMEOUT_SECONDS):
    project_root = os.path.abspath(project_root or os.getcwd())
    config = runtime_isolation.read_config() if config is None else config
    static = build_static(project_root, plugin_root_path, config)
    report = {"static": static, "probe": None, "guard_log": summarize_guard_log(project_root)}
    if probe and static["isolation"]:
        report["probe"] = run_probe(static, config, project_root, timeout)
    return report


def format_counts(counts):
    return ", ".join("%s=%d" % (key, counts[key]) for key in sorted(counts, key=lambda item: (-counts[item], item))) or "(none)"


def format_report(report):
    static, probe, guard = report["static"], report["probe"], report["guard_log"]
    out = ["== access report: %s" % static["project"],
           "isolation ........ %s" % ("on" if static["isolation"] else "OFF (loop_isolation=false: today's argv, nothing below is enforced)"),
           "plugin root ...... %s" % (static["plugin_root"] or "(not found)"),
           "toolkit root ..... %s" % (static.get("toolkit_root") or "(not found: /plugin install claude-dev-toolkit@claude-dev-toolkit)"),
           "guard ............ %s" % (static["guard"] or "(not found)"), "", "-- MCP servers present (read tools only)"]
    out += ["  %s: %s" % (row["server"], ", ".join(name.rsplit("__", 1)[-1] for name in row["tools"])) for row in static["mcp_servers"]]
    out += [] if static["mcp_servers"] else ["  (none)"]
    out.append("-- MCP tools allowed by profile (guard MCP_ALLOW; Grafana reads pass by prefix)")
    for profile, tools in static["mcp_allow_by_profile"].items():
        out.append("  %-13s %s" % (profile, ", ".join(tools) or "none"))
    out += [] if static["mcp_allow_by_profile"] else ["  (guard not loaded)"]
    out.append("-- skills allowlist by profile")
    for profile, skills in sorted(static["skills_allowlist"].items()):
        out.append("  %-13s %s" % (profile, ", ".join(label_skill(name, static.get("slot_skills") or {}) for name in skills)))
    bundle = static["bundle"]
    if bundle:
        out += ["-- bundle %s%s" % (bundle["path"], " (rebuilt)" if bundle.get("rebuilt") else ""),
                "  agents:  %s" % (", ".join(bundle["agents"]) or "(none)"), "  skills:  %s" % (", ".join(label_skill(name, static.get("slot_skills") or {}) for name in bundle["skills"]) or "(none)"),
                "  skipped: %s" % (", ".join(bundle["skipped"]) or "(none)")]
        if bundle.get("error"):
            out.append("  ERROR:   %s" % bundle["error"])
        out.append("  allowlist file: %s" % static["allowlist_file"])
    out += ["", "-- probe (real isolated argv, killed at the init event)"]
    if probe is None:
        out.append("  skipped (--no-probe or isolation off)")
    elif not probe["ok"]:
        out.append("  FAILED: %s" % probe["error"])
    else:
        out += ["  model:       %s" % probe["model"], "  tools:       %d" % probe["tool_count"],
                "  mcp tools:   %s" % (", ".join(probe["mcp_tools"]) or "(none)"),
                "  mcp servers: %s" % (", ".join("%s(%s)" % (short_name(item), item.get("status", "?") if isinstance(item, dict) else "?")
                                                 for item in probe["mcp_servers"]) or "(none)"),
                "  agents:      %s" % (", ".join(probe["agents"]) or "(none)"), "  skills:      %s" % (", ".join(probe["skills"]) or "(none)"),
                "  plugins:     %s" % (", ".join(short_name(item) for item in probe["plugins"]) or "(none besides builtin)")]
        out += ["  FLAG: %s" % flag for flag in probe["flags"]] or []
        out.append("  no flags: everything loaded is inside the allowlists" if not probe["flags"] else "  %d flag(s)" % len(probe["flags"]))
    out += ["", "-- guard.log (last %d lines) %s" % (GUARD_LOG_LINES, guard["path"] or "(no log)")]
    if guard["lines"]:
        out += ["  lines: %d" % guard["lines"], "  by decision: %s" % format_counts(guard["by_decision"]),
                "  by rule:     %s" % format_counts(guard["by_rule"]), "  by agent:    %s" % format_counts(guard["by_agent"])]
        out += ["  last denials:"] + ["    %s [%s/%s] %s" % (item["t"], item["profile"], item["rule"], item["detail"]) for item in guard["last_denials"]]
        out += [] if guard["last_denials"] else ["    (none)"]
    return "\n".join(out)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="harness.py doctor --access", description="What a `claude -p` of the app can reach")
    parser.add_argument("--project", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-probe", action="store_true")
    options = parser.parse_args(argv)
    project = runtime_isolation.project_root_of(options.project) if options.project else runtime_isolation.project_root_of(os.getcwd())
    report = build_report(project, probe=not options.no_probe)
    print(json.dumps(report, ensure_ascii=False, indent=2) if options.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
