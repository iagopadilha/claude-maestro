#!/usr/bin/env python3
"""Task waves of ONE feature (stdlib only): parses `tasks.md`, builds the waves and keeps the task status.

  python task_waves.py waves <slug> [--max N] [--root DIR]      JSON: waves (task ids) of the tasks that are not done yet
  python task_waves.py set <slug> <Tn> <pending|running|done|failed> [--note TEXT] [--root DIR]
  python task_waves.py mirror <slug> [--root DIR]               copies the status into the `**Status**:` lines of tasks.md

Also reachable as `harness.py tasks ...`. Root = the folder that holds `.specs` (--root, else the current folder walking up).
Status file: `.specs/project/loop/internal/tasks/<slug>.json`. Format of tasks.md: `app/templates/specs/TASKS.md`.
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import loop_state  # noqa: E402  (feature folder, ROADMAP parser, loop folder layout)
import app_paths  # noqa: E402,F401  (puts the harness_backend package on sys.path)
from harness_backend.domain.roadmap_settings import parse_loop_settings  # noqa: E402

TEXT_ENCODING = "utf-8"
TASKS_FILE_NAME = "tasks.md"
TASKS_STATUS_FOLDER = "tasks"
ROADMAP_RELATIVE = (".specs", "project", "ROADMAP.md")
STATUS_VALUES = ("pending", "running", "done", "failed")
TASK_TYPES = ("impl", "wiring")
DEFAULT_TYPE = "impl"
NO_DEPENDENCIES_WORD = "none"
SYNTHETIC_TASK_ID = "T1"
TASK_HEADING = re.compile(r"^###\s+(T\d+)\s*:\s*(.*?)\s*$", re.I)
PARALLEL_MARK = re.compile(r"\s*\[P\]\s*$", re.I)
FIELD_LINE = re.compile(r"^\*\*(Type|Files|Deps|Reqs|Done when|Status)(?:\*\*\s*:|\s*:\*\*)\s*(.*?)\s*$", re.I)
STATUS_FIELD_LINE = re.compile(r"^(\*\*Status(?:\*\*\s*:|\s*:\*\*)\s*)(\S*)(.*)$", re.I)
HTML_COMMENT = re.compile(r"<!--.*?-->")
TASK_ID = re.compile(r"T\d+", re.I)
EXIT_OK = 0
EXIT_PROBLEM = 1


def split_list(value):
    cleaned = [item.strip().strip("`").strip() for item in HTML_COMMENT.sub("", value).split(",")]
    return [item for item in cleaned if item and item.lower() != NO_DEPENDENCIES_WORD]


def parse_tasks(text):
    """TASKS.md -> {"tasks": [{id, title, parallel, type, files, deps, reqs, done_when, status}], "problems": [str]}."""
    tasks, problems, current = [], [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        heading = TASK_HEADING.match(line)
        if heading:
            title = heading.group(2)
            parallel = bool(PARALLEL_MARK.search(title))
            current = {"id": heading.group(1).upper(), "title": PARALLEL_MARK.sub("", title).strip(), "parallel": parallel,
                       "type": DEFAULT_TYPE, "files": [], "deps": [], "reqs": [], "done_when": "", "status": "pending"}
            tasks.append(current)
            continue
        if line.startswith("#"):
            current = None
            continue
        field = FIELD_LINE.match(line)
        if current is None or not field:
            continue
        name, value = field.group(1).lower(), HTML_COMMENT.sub("", field.group(2)).strip()
        if name == "type":
            current["type"] = value.lower() or DEFAULT_TYPE
        elif name == "files":
            current["files"] = split_list(value)
        elif name == "deps":
            current["deps"] = [item.upper() for item in split_list(value)]
        elif name == "reqs":
            current["reqs"] = split_list(value)
        elif name == "done when":
            current["done_when"] = value
        elif name == "status":
            current["status"] = value.lower() or "pending"
    seen = set()
    for task in tasks:
        if task["id"] in seen:
            problems.append("duplicate task id %s" % task["id"])
        seen.add(task["id"])
        if task["type"] not in TASK_TYPES:
            problems.append("%s: Type must be impl or wiring (found %r)" % (task["id"], task["type"]))
        if not task["done_when"]:
            problems.append("%s: no Done when" % task["id"])
    return {"tasks": tasks, "problems": problems}


def build_waves(tasks, max_parallel, done=frozenset()):
    """-> {"waves": [[task_id, ...]], "problems": [str]} for the tasks that are not in `done`.

    A task enters the earliest wave where every dependency is done or in an earlier wave, none of its files is used by another task of
    that wave and the wave is below `max_parallel`. A wiring task waits for every dependency it has (it is always listed after them).
    Unknown dependency, duplicate id or cycle = a problem and no waves."""
    done = set(done)
    known = [task["id"] for task in tasks]
    problems = ["duplicate task id %s" % task_id for task_id in sorted({item for item in known if known.count(item) > 1})]
    for task in tasks:
        for dependency in task["deps"]:
            if dependency not in known:
                problems.append("%s depends on unknown task %s" % (task["id"], dependency))
            elif dependency == task["id"]:
                problems.append("%s depends on itself" % task["id"])
    if problems:
        return {"waves": [], "problems": problems}
    capacity = max(1, int(max_parallel))
    remaining = [task for task in tasks if task["id"] not in done]
    finished, waves = set(done), []
    while remaining:
        wave, wave_files = [], set()
        for task in remaining:
            if not all(dependency in finished for dependency in task["deps"]) or len(wave) >= capacity:
                continue
            files = {loop_state.norm(path) for path in task["files"]}
            if files & wave_files:
                continue
            wave.append(task)
            wave_files |= files
        if not wave:
            stuck = ", ".join(task["id"] for task in remaining)
            return {"waves": [], "problems": ["dependency cycle among: %s" % stuck]}
        waves.append([task["id"] for task in wave])
        finished |= {task["id"] for task in wave}
        remaining = [task for task in remaining if task["id"] not in finished]
    return {"waves": waves, "problems": []}


# ---------------------------------------------------------------- status file
def status_path(root, slug):
    return os.path.join(loop_state.internal_dir(root), TASKS_STATUS_FOLDER, loop_state.feature_folder_name(slug) + ".json")


def read_status(root, slug):
    """{"slug", "updated_at", "tasks": {Tn: {status, wave, note}}}; a missing or unreadable file = no tasks."""
    empty = {"slug": slug, "updated_at": "", "tasks": {}}
    try:
        with open(status_path(root, slug), encoding=TEXT_ENCODING) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
        return empty
    data.setdefault("slug", slug)
    return data


def write_status(root, slug, task_id, status, note="", wave=None):
    """Sets one task in the status file (atomic write) and returns the whole document."""
    if status not in STATUS_VALUES:
        raise ValueError("status must be one of: %s" % ", ".join(STATUS_VALUES))
    document = read_status(root, slug)
    previous = document["tasks"].get(task_id, {}) if isinstance(document["tasks"].get(task_id), dict) else {}
    document["tasks"][task_id] = {"status": status, "wave": wave if wave is not None else previous.get("wave"), "note": note}
    document["updated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = status_path(root, slug)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".tasks-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding=TEXT_ENCODING) as stream:
            json.dump(document, stream, indent=2)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    return document


def mirror_status_into_tasks_md(feature_dir, status):
    """Rewrites only the `**Status**:` line of each task block with the status of `status` (the document of `read_status`).
    Returns how many lines changed; a task without a recorded status or without a Status line stays as it is."""
    path = os.path.join(feature_dir, TASKS_FILE_NAME)
    try:
        with open(path, encoding=TEXT_ENCODING, newline="") as handle:
            text = handle.read()
    except OSError:
        return 0
    recorded = status.get("tasks", {}) if isinstance(status, dict) else {}
    current, changed, lines = None, 0, text.splitlines(keepends=True)
    for index, raw in enumerate(lines):
        stripped = raw.rstrip("\r\n")
        heading = TASK_HEADING.match(stripped.strip())
        if heading:
            current = heading.group(1).upper()
            continue
        found = STATUS_FIELD_LINE.match(stripped.strip())
        entry = recorded.get(current) if current else None
        if not found or not isinstance(entry, dict) or entry.get("status") not in STATUS_VALUES:
            continue
        if found.group(2).lower() == entry["status"]:
            continue
        ending = raw[len(stripped):]
        lines[index] = found.group(1) + entry["status"] + found.group(3) + ending
        changed += 1
    if changed:
        with open(path, "w", encoding=TEXT_ENCODING, newline="") as handle:
            handle.write("".join(lines))
    return changed


# ---------------------------------------------------------------- feature level
def find_root(start):
    current = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(current, ".specs")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start)
        current = parent


def read_default_max_parallel(root):
    return parse_loop_settings(loop_state.read(os.path.join(root, *ROADMAP_RELATIVE)))["settings"]["max_parallel_tasks"]


def load_feature_tasks(root, slug):
    """{"tasks", "problems"} from the feature's tasks.md; a legacy feature without it gets one synthetic task over its ROADMAP paths."""
    path = os.path.join(loop_state.feature_dir(root, slug), TASKS_FILE_NAME)
    if os.path.isfile(path):
        return parse_tasks(loop_state.read(path))
    features, _ = loop_state.parse_roadmap(loop_state.read(os.path.join(root, *ROADMAP_RELATIVE)))
    paths = [item for item in (features.get(slug, {}).get("paths") or "").split(",") if item.strip()]
    synthetic = {"id": SYNTHETIC_TASK_ID, "title": "Build the whole feature", "parallel": False, "type": DEFAULT_TYPE, "files": paths,
                 "deps": [], "reqs": [], "done_when": "the spec's acceptance criteria hold", "status": "pending"}
    return {"tasks": [synthetic], "problems": ["no tasks.md for %s: one synthetic task %s covers the feature paths" % (slug, SYNTHETIC_TASK_ID)]}


def plan_wave_numbers(tasks, max_parallel):
    """{task_id: wave number from 1} of the full plan (nothing done), so a task keeps its wave while others finish."""
    built = build_waves(tasks, max_parallel)
    return {task_id: number for number, wave in enumerate(built["waves"], 1) for task_id in wave}


def describe_waves(root, slug, max_parallel=None):
    parsed = load_feature_tasks(root, slug)
    limit = max_parallel or read_default_max_parallel(root)
    status = read_status(root, slug)
    done = {task_id for task_id, entry in status["tasks"].items() if isinstance(entry, dict) and entry.get("status") == "done"}
    known = {task["id"] for task in parsed["tasks"]}
    built = build_waves(parsed["tasks"], limit, done & known)
    return {"slug": slug, "max_parallel": limit, "waves": built["waves"], "done": sorted(done & known),
            "tasks": [task for task in parsed["tasks"] if task["id"] not in done], "problems": parsed["problems"] + built["problems"]}


def set_task_status(root, slug, task_id, status, note=""):
    parsed = load_feature_tasks(root, slug)
    task_id = task_id.upper()
    limit = read_default_max_parallel(root)
    wave = plan_wave_numbers(parsed["tasks"], limit).get(task_id)
    return write_status(root, slug, task_id, status, note, wave)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="harness.py tasks", description="Task waves of one feature")
    commands = parser.add_subparsers(dest="command", required=True)
    waves = commands.add_parser("waves", help="waves of the tasks that are not done")
    waves.add_argument("slug")
    waves.add_argument("--max", type=int, default=None, help="tasks per wave (default: Max parallel tasks of the ROADMAP, else 3)")
    setter = commands.add_parser("set", help="records the status of one task")
    setter.add_argument("slug")
    setter.add_argument("task")
    setter.add_argument("status", choices=STATUS_VALUES)
    setter.add_argument("--note", default="")
    mirror = commands.add_parser("mirror", help="writes the status into the Status lines of tasks.md")
    mirror.add_argument("slug")
    for command in (waves, setter, mirror):
        command.add_argument("--root", default="", help="folder that holds .specs (default: the current folder, walking up)")
    options = parser.parse_args(argv)
    root = os.path.abspath(options.root) if options.root else find_root(os.getcwd())
    if options.command == "waves":
        result = describe_waves(root, options.slug, options.max)
        print(json.dumps(result, indent=2))
        return EXIT_PROBLEM if result["problems"] and not result["waves"] and result["tasks"] else EXIT_OK
    if options.command == "set":
        document = set_task_status(root, options.slug, options.task, options.status, options.note)
        print(json.dumps(document, indent=2))
        return EXIT_OK
    changed = mirror_status_into_tasks_md(loop_state.feature_dir(root, options.slug), read_status(root, options.slug))
    print(json.dumps({"slug": options.slug, "lines_changed": changed}))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
