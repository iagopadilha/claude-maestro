#!/usr/bin/env python3
"""Single CLI of Loop Harness (stdlib only).

  python harness.py serve [--project <repo>] [--port N] [--idle-hours H] [--no-browser] [--data-dir D]
      starts the app (multi-project panel). If an app is already alive (app.json), it only registers the project in it,
      prints MONITOR_URL=<project url> and exits 0.
  python harness.py demo [--port N] [--no-browser] [--speed S] [--data-dir D] [--loop] [--keep]
      opens the panel on a fake project ("acme-checkout") with mocked, moving loop data; never calls claude and never touches
      your data folder (everything lives in a temporary folder, or in <data-dir>/demo/). Ctrl+C stops it and removes the files.
  python harness.py loop <repo> [run_loop flags...]   runs the loop (engine/run_loop.py): --wave, --check, --answer, --option, --rewind...
  python harness.py check <repo> [flags...]           shortcut for `loop <repo> --check`
  python harness.py state|notify|summary|deliver|rules|audit|graph|classify|scale|inventory|tasks [args...]   passed on to the engine scripts
      tasks waves <slug> [--max N] | set <slug> <Tn> <status> [--note] | mirror <slug>   task waves of one feature (--root DIR)
  python harness.py standards <repo> [--subproject <path>] [--set slot=skill ...] [--clear slot ...] [--json]
      shows or edits the Standards slots (git, backend, frontend, app-map, observability) of the `## Standards` section of the
      project's CLAUDE.md (in a monorepo: of each subproject's CLAUDE.md). Never invents a value.
  python harness.py paths [--json]                    the Maestro plugin root, the orchestrator skill folder and the claude-dev-toolkit
      root (empty when missing; exit 0 anyway): the agents run it to find `<toolkit>/shared/<script>`
  python harness.py doctor [--json] [--project <repo>]                   diagnostics (the same as GET /api/app/info); FAILs when
      claude-dev-toolkit is not found; with --project, WARNs for each Standards slot whose skill is not installed
  python harness.py doctor --access [--project <repo>] [--json] [--no-probe]   what a `claude -p` can reach: MCP, skills, bundle, probe, guard.log
      (the probe starts the real isolated claude with a 0.05 USD cap and kills it at the init event; always exit 0, it is a report)
  python harness.py install [--data-dir D] [--skill-dir S]   writes ~/.loop-harness/config.json (app_root_dir and the skill
                                                      folder); this is how the skill finds the app. Does not touch ~/.claude.
Exit code: the one of the script passed on; `serve` 0 when it ends; `doctor` 1 when there is a problem.
"""
import argparse
import json
import os
import subprocess
import sys

APP_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(APP_ROOT_DIR, "backend")
ENGINE_DIR = os.path.join(BACKEND_DIR, "harness_backend", "engine")
for _folder in (BACKEND_DIR, ENGINE_DIR):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

FORWARDED_SCRIPTS = {"loop": "run_loop", "state": "loop_state", "notify": "notify", "summary": "loop_summary", "deliver": "delivery",
                     "rules": "rules", "audit": "audit_deps", "graph": "graph_view", "classify": "classify_structure",
                     "scale": "scale", "inventory": "code_inventory", "tasks": "task_waves", "standards": "standards"}
TOOLKIT_INSTALL_LINES = ("/plugin marketplace add iagopadilha/claude-dev-toolkit", "/plugin install claude-dev-toolkit@claude-dev-toolkit")
TOOLKIT_PROBLEM_PREFIX = "claude-dev-toolkit not found"
DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
DISABLED_VALUES = ("off", "0", "false", "no")
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID_USAGE = 2


def run_engine_script(script_name, args):
    """Passes argv to the engine script with the same Python, inheriting stdin/stdout/stderr. Returns the exit code."""
    path = os.path.join(ENGINE_DIR, script_name + ".py")
    try:
        return subprocess.call([sys.executable, path] + list(args))
    except KeyboardInterrupt:
        return EXIT_FAILURE


def apply_data_dir(data_dir):
    """--data-dir applies to this process and to the children (engine, relaunched loop)."""
    if data_dir:
        os.environ[DATA_DIR_VARIABLE] = os.path.abspath(data_dir)


def is_browser_disabled_by_environment():
    from harness_backend.config import envload
    return str(envload.get("LOOP_MONITOR_OPEN") or "").strip().lower() in DISABLED_VALUES


def register_project_in_live_app(project_path):
    """Live app (app.json + pid + health): registers the project and returns (project or hub url, app url). No app: (None, None)."""
    import monitor_proc
    app = monitor_proc.read_live_app()
    if not app:
        return None, None
    if not project_path:
        return app["url"], app["url"]
    url = monitor_proc.register_project_in_app(app, os.path.abspath(project_path))
    return url, app["url"]


def run_serve(args):
    parser = argparse.ArgumentParser(prog="harness.py serve", description="Starts the multi-project panel (or reuses the live app)")
    parser.add_argument("--project", default="", help="project folder to open (registers it in the app)")
    parser.add_argument("--port", type=int, default=0, help="fixed port (default: first free one from 47800 to 47899)")
    parser.add_argument("--idle-hours", type=float, default=None, help="hours without use until it shuts down on its own (0 = never)")
    parser.add_argument("--no-browser", action="store_true", help="does not open the browser when it starts")
    parser.add_argument("--data-dir", default="", help="app data folder (default: ~/.loop-harness or LOOP_DATA_DIR)")
    options = parser.parse_args(args)
    apply_data_dir(options.data_dir)
    if options.project and not os.path.isdir(options.project):
        print("project folder does not exist: %s" % options.project, file=sys.stderr)
        return EXIT_INVALID_USAGE
    project_url, app_url = register_project_in_live_app(options.project)
    if app_url:
        if not project_url:
            print("the live app at %s did not register the project %s" % (app_url, options.project), file=sys.stderr)
            return EXIT_FAILURE
        print("MONITOR_URL=" + project_url, flush=True)
        print("APP_URL=" + app_url, flush=True)
        return EXIT_OK
    try:
        from harness_backend import composition
    except ImportError as failure:
        print("the app is not complete in this folder (%s): %s" % (type(failure).__name__, failure), file=sys.stderr)
        return EXIT_FAILURE
    overrides = {"user_data_dir": os.environ.get(DATA_DIR_VARIABLE, ""), "fixed_port": options.port,
                 "open_browser": not (options.no_browser or is_browser_disabled_by_environment())}
    if options.idle_hours is not None:
        overrides["idle_hours"] = options.idle_hours
    application = composition.build_application(**overrides)
    try:
        composition.start_application(application, initial_project_path=os.path.abspath(options.project) if options.project else "")
    except KeyboardInterrupt:
        pass
    except OSError as failure:
        print("could not start the app: %s" % failure, file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


def run_demo(args):
    try:
        from harness_backend.demo.demo_runner import run_demo as run_demo_mode
    except ImportError as failure:
        print("the app is not complete in this folder (%s): %s" % (type(failure).__name__, failure), file=sys.stderr)
        return EXIT_FAILURE
    return run_demo_mode(args)


def build_diagnostics(project_folders=()):
    from harness_backend.config.app_config import load_app_config
    from harness_backend.services.app_diagnostics_service import build_app_diagnostics_service
    import monitor_proc
    config = load_app_config()
    diagnostics = build_app_diagnostics_service(config).build_diagnostics(project_folders)
    app = monitor_proc.read_live_app()
    diagnostics["live_app"] = {"url": app["url"], "pid": app.get("pid")} if app else None
    return diagnostics


def print_diagnostics(diagnostics):
    from harness_backend.services.claude_account_service import format_usage_line
    def mark(value):
        return "ok " if value else "NO "
    guard = diagnostics["guard"]
    print("app folder ....... %s" % diagnostics["app_root_dir"])
    print("data folder ...... %s" % diagnostics["data_dir"])
    print("skill ............ %s" % (diagnostics["skill_dir"] or "(not found)"))
    toolkit = diagnostics.get("toolkit") or {}
    print("toolkit .......... %s" % (toolkit.get("path") or "(not found)"))
    print("[%s] guard found (%s)" % (mark(guard["found"]), guard["path"] or "-"))
    print("[%s] guard blocks `git push`" % mark(guard["blocks_git_push"]))
    claude = diagnostics["claude"]
    print("[%s] `claude` available" % mark(claude["on_path"]))
    print("claude version ... %s (JSON contract tested with %s)" % (claude.get("version") or "unknown", claude.get("min_tested_version", "?")))
    for command in diagnostics.get("claude_commands", []):
        state = command["label"] or (command["auth"].get("error") or "account unknown") if command["found"] else "not found (skipped, never blocks)"
        print("[%s] %-8s `%s`: %s" % (mark(command["found"]), command["role"], command["name"], state))
    for usage in diagnostics.get("claude_usage", []):
        print("      usage %-14s %s%s" % (usage["key"], format_usage_line(usage), "" if usage["age_seconds"] is None else " (read %d min ago)" % (usage["age_seconds"] // 60)))
    print("[%s] .env file found" % mark(diagnostics["env_loaded"]))
    for channel in diagnostics["notify_channels"]:
        print("[%s] channel %-9s enabled=%s required=%s" % (mark(channel["configured"]), channel["name"], channel["enabled"], channel["required"]))
    app = diagnostics.get("live_app")
    print("live app ......... %s" % ("%s (pid %s)" % (app["url"], app["pid"]) if app else "none"))
    for row in diagnostics.get("standards", []):
        print("[%s] standards %-13s %s (%s)" % (mark(row["resolved"]), row["slot"], row["skill"], row["folder"]))
    for warning in diagnostics.get("warnings", []):
        print("WARNING:", warning)
    for problem in diagnostics["problems"]:
        print("PROBLEM:", problem)
    if not toolkit.get("found", True):
        print("  install it in Claude Code:")
        for line in TOOLKIT_INSTALL_LINES:
            print("    " + line)
    print("all good" if not diagnostics["problems"] else "%d problem(s)" % len(diagnostics["problems"]))


def run_doctor(args):
    parser = argparse.ArgumentParser(prog="harness.py doctor", description="App diagnostics (skill, guard, claude, notify channels)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--project", default="", help="repo folder whose loop files tell the 5-hour/weekly usage (default: usage unknown)")
    parser.add_argument("--access", action="store_true", help="access report (MCP, skills, bundle, probe of the isolated claude, guard.log)")
    parser.add_argument("--no-probe", action="store_true", help="with --access: do not start claude")
    parser.add_argument("--toolkit-optional", action="store_true", help="a missing claude-dev-toolkit is a warning, not a problem (install.py uses it)")
    options = parser.parse_args(args)
    apply_data_dir(options.data_dir)
    if options.access:
        import access_report
        report = access_report.build_report(options.project or os.getcwd(), probe=not options.no_probe)
        print(json.dumps(report, ensure_ascii=False, indent=2) if options.json else access_report.format_report(report))
        return EXIT_OK
    diagnostics = build_diagnostics([os.path.abspath(options.project)] if options.project else [])
    if options.toolkit_optional:
        missing = [problem for problem in diagnostics["problems"] if problem.startswith(TOOLKIT_PROBLEM_PREFIX)]
        diagnostics["problems"] = [problem for problem in diagnostics["problems"] if problem not in missing]
        diagnostics["warnings"] = list(diagnostics.get("warnings", [])) + missing
    if options.json:
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    else:
        print_diagnostics(diagnostics)
    return EXIT_FAILURE if diagnostics["problems"] else EXIT_OK


def build_paths():
    """{"maestro_plugin", "orchestrator_skill", "toolkit"}: the plugin root with the orchestrator, the skill folder and the toolkit root ('' when missing)."""
    from harness_backend.config import app_config, skill_locator
    config = app_config.read_user_config(app_config.get_user_data_dir())
    skill_dir = skill_locator.locate_orchestrator_skill_dir(config.get("orchestrator_skill_dir", ""))
    return {"maestro_plugin": skill_locator.plugin_root_of(skill_dir), "orchestrator_skill": skill_dir,
            "toolkit": skill_locator.find_toolkit_root(config)}


def run_paths(args):
    parser = argparse.ArgumentParser(prog="harness.py paths", description="Where the Maestro plugin, the orchestrator skill and the dev toolkit are")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--data-dir", default="")
    options = parser.parse_args(args)
    apply_data_dir(options.data_dir)
    paths = build_paths()
    if options.json:
        print(json.dumps(paths, ensure_ascii=False, indent=2))
    else:
        for key, value in paths.items():
            print("%s=%s" % (key, value))
    return EXIT_OK


def run_install(args):
    from harness_backend.config import app_config, skill_locator
    parser = argparse.ArgumentParser(prog="harness.py install", description="Writes the app config.json (how the skill finds the app)")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--skill-dir", default="", help="orchestrator skill folder (default: detected)")
    options = parser.parse_args(args)
    apply_data_dir(options.data_dir)
    data_dir = app_config.get_user_data_dir()
    config = app_config.read_user_config(data_dir)
    skill_dir = skill_locator.locate_orchestrator_skill_dir(options.skill_dir or config.get("orchestrator_skill_dir", ""))
    config["app_root_dir"] = APP_ROOT_DIR
    if skill_dir:
        config["orchestrator_skill_dir"] = skill_dir
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "config.json")
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
    print("written: %s" % path)
    print("  app_root_dir = %s" % APP_ROOT_DIR)
    print("  orchestrator_skill_dir = %s" % (skill_dir or "(skill not found: run again with --skill-dir)"))
    return EXIT_OK if skill_dir else EXIT_FAILURE


def print_help():
    print(__doc__.strip())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print_help()
        return EXIT_OK if argv else EXIT_INVALID_USAGE
    command, args = argv[0], argv[1:]
    if command == "serve":
        return run_serve(args)
    if command == "demo":
        return run_demo(args)
    if command == "check":
        if not args:
            print("usage: harness.py check <repo> [run_loop flags...]", file=sys.stderr)
            return EXIT_INVALID_USAGE
        return run_engine_script("run_loop", [args[0], "--check"] + args[1:])
    if command in FORWARDED_SCRIPTS:
        return run_engine_script(FORWARDED_SCRIPTS[command], args)
    if command == "doctor":
        return run_doctor(args)
    if command == "install":
        return run_install(args)
    if command == "paths":
        return run_paths(args)
    print("unknown command: %s\n" % command, file=sys.stderr)
    print_help()
    return EXIT_INVALID_USAGE


if __name__ == "__main__":
    sys.exit(main())
