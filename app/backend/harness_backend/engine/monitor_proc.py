#!/usr/bin/env python3
"""Ensures the web panel (the Loop Harness app) for the loop and writes the command to relaunch the loop. Stdlib only.

The panel is the multi-project app (`harness.py serve`): one single process for every project, INDEPENDENT of run_loop.py
(it starts detached, does not read stdin and does not die when the loop ends). Order of `ensure_monitor`:
  1. LOOP_MONITOR_URL (the panel that launched this loop) answering `<url>api/health` -> use it.
  2. The work's MONITOR.json with a live pid and `<url>api/health` returning the SAME work -> reuse it (the app writes a
     compatible one: project url + app pid).
  3. app.json (LOOP_DATA_DIR or ~/.loop-harness) with a live pid and `GET <url>api/app/health` ok -> register the project:
     `POST <url>api/app/projects/open {"path": repo}` (header X-Monitor-Token from app.json) and use the url returned.
  4. App not alive -> start `python <root>/harness.py serve --project <repo>` detached (LOOP_MONITOR_SERVER swaps the script,
     for tests) and read `MONITOR_URL=` from the log (or the MONITOR.json the app writes).
Everything is best effort: any failure returns None and prints a warning; the loop never falls because of the panel.

Files in <work>/.specs/project/loop/:
  json/MONITOR.json      written by the app when it opens the project (app pid, project url, repo, work); deleted by it on exit
  json/LOOP-CMD.json     written here on every run_loop.py run: {"argv": [...], "cwd": "..."}; without --answer/--option
  internal/monitor.log   output of the `harness.py serve` launched from here
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app_paths  # noqa: E402  (harness_backend package on sys.path)
import loop_state  # noqa: E402  (the loop folder name in one place)
from harness_backend.config.app_config import get_user_data_dir  # noqa: E402
from harness_backend.infrastructure.process_runner import ProcessRunner  # noqa: E402

IS_WIN = os.name == "nt"
DEFAULT_WAIT_SECONDS = 15.0
POLL_SECONDS = 0.2
HEALTH_TIMEOUT_SECONDS = 1.5
REGISTER_TIMEOUT_SECONDS = 10
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DISABLED_VALUES = ("off", "0", "false", "no")
MONITOR_URL_VARIABLE = "LOOP_MONITOR_URL"  # set by the panel when it launches the loop: reuse this panel
SERVER_OVERRIDE_VARIABLE = "LOOP_MONITOR_SERVER"  # alternative script instead of harness.py (tests and custom installs)
MONITOR_URL_PREFIX = "MONITOR_URL="
APP_REGISTRY_NAME = "app.json"
TOKEN_HEADER = "X-Monitor-Token"
PROCESSES = ProcessRunner()


def loop_folder(work):
    return loop_state.loop_dir(work)


def monitor_file(work):
    return loop_state.loop_path(work, "MONITOR.json")


def app_cli_path():
    """harness.py at the app root (the same checkout as this engine)."""
    return os.path.join(app_paths.APP_ROOT_DIR, "harness.py")


def default_server_path():
    return os.environ.get(SERVER_OVERRIDE_VARIABLE) or app_cli_path()


def app_registry_path():
    return os.path.join(get_user_data_dir(), APP_REGISTRY_NAME)


def browser_open_disabled(environment_value):
    """LOOP_MONITOR_OPEN=off (env or the app .env) starts the app with --no-browser."""
    return monitor_disabled_by_environment(environment_value)


def monitor_disabled_by_environment(environment_value):
    """LOOP_MONITOR=off (or 0/false/no) in the app .env turns the panel off for every loop on this machine."""
    return str(environment_value or "").strip().lower() in DISABLED_VALUES


def same_path(first, second):
    def normalize(path):
        return os.path.normcase(os.path.realpath(os.path.abspath(str(path or ""))))
    return normalize(first) == normalize(second)


def process_is_alive(pid):
    return PROCESSES.is_alive(pid)


def read_json_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_monitor_info(work):
    info = read_json_file(monitor_file(work))
    return info if info and info.get("url") and info.get("pid") else None


def local_endpoint(url, relative):
    """`<url>` + `relative` (project url `/p/<id>/` -> `/p/<id>/api/health`). None outside 127.0.0.1/localhost."""
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in LOCAL_HOSTS:
        return None
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return urllib.parse.urljoin(urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")), relative)


def request_local_json(url, relative, body=None, token="", timeout=HEALTH_TIMEOUT_SECONDS):
    """GET (or POST with `body`) on a local endpoint, without a proxy. Returns the response dict or None."""
    address = local_endpoint(url, relative)
    if not address:
        return None
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(address, data=data, method="POST" if body is not None else "GET")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header(TOKEN_HEADER, token)
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost never goes through a proxy
    try:
        with direct.open(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return answer if isinstance(answer, dict) else None


def monitor_answers_health(info, work):
    """GET <url>api/health answers ok for the SAME work. Only talks to 127.0.0.1/localhost."""
    body = request_local_json(info.get("url") if isinstance(info, dict) else None, "api/health")
    return bool(body) and body.get("ok") is True and same_path(body.get("work"), work)


def local_url_answers_health(url):
    """GET <url>api/health answers ok=true. Only 127.0.0.1/localhost, without a proxy."""
    body = request_local_json(url, "api/health")
    return bool(body) and body.get("ok") is True


def monitor_url_from_environment():
    """URL in LOOP_MONITOR_URL (the panel that launched this loop) if it is alive; otherwise None."""
    url = os.environ.get(MONITOR_URL_VARIABLE, "").strip()
    return url if url and local_url_answers_health(url) else None


def running_monitor_url(work):
    """URL of this work's panel that is already alive (live pid and health with the same work) or None."""
    info = read_monitor_info(work)
    if info and process_is_alive(info.get("pid")) and monitor_answers_health(info, work):
        return str(info["url"])
    return None


def read_live_app():
    """app.json of the live app (live pid + GET <url>api/app/health ok) or None."""
    registry = read_json_file(app_registry_path())
    if not registry or not registry.get("url") or not process_is_alive(registry.get("pid")):
        return None
    health = request_local_json(registry["url"], "api/app/health")
    return registry if health and health.get("ok") is True else None


def register_project_in_app(app, repo):
    """POST <url>api/app/projects/open {"path": repo} -> ABSOLUTE url of the project panel, or None."""
    answer = request_local_json(app["url"], "api/app/projects/open", body={"path": repo}, token=str(app.get("token") or ""),
                                timeout=REGISTER_TIMEOUT_SECONDS)
    if not answer or answer.get("ok") is not True or not answer.get("url"):
        return None
    return urllib.parse.urljoin(local_endpoint(app["url"], "") or str(app["url"]), str(answer["url"]))


def launch_app(repo, work, server_path, python, open_browser=True):
    """Starts `python <harness.py> serve --project <repo>` detached; output in internal/monitor.log. Returns (pid, log offset)."""
    log_path = loop_state.loop_path(work, "monitor.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    command = [python, server_path, "serve", "--project", repo] + ([] if open_browser else ["--no-browser"])  # the app opens the browser once, only when it starts
    pid = PROCESSES.spawn_detached(command, os.path.dirname(os.path.abspath(server_path)), log_path)
    return pid, offset


def monitor_url_from_log(work, offset):
    """Last `MONITOR_URL=` line written to monitor.log after `offset`."""
    try:
        with open(loop_state.loop_path(work, "monitor.log"), "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    urls = [line.strip()[len(MONITOR_URL_PREFIX):].strip() for line in text.splitlines() if line.strip().startswith(MONITOR_URL_PREFIX)]
    return urls[-1] if urls and urls[-1] else None


def answered_url(work, offset):
    """URL the app already announced (MONITOR_URL= in the log or the work's MONITOR.json) that answers health; otherwise None."""
    from_log = monitor_url_from_log(work, offset)
    if from_log and local_url_answers_health(from_log):
        return from_log
    info = read_monitor_info(work)
    if info and same_path(info.get("work"), work) and monitor_answers_health(info, work):
        return str(info["url"])
    return None


def wait_for_monitor(work, pid, offset, wait_seconds):
    deadline = time.time() + wait_seconds
    while True:
        url = answered_url(work, offset)
        if url:
            return url
        if not process_is_alive(pid):  # it exited: either it did not start, or a live app existed (race) and it only registered the project and printed the URL
            return answered_url(work, offset) or running_monitor_url(work)
        if time.time() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


def ensure_monitor(repo, work, server_path=None, wait_seconds=DEFAULT_WAIT_SECONDS, python=None, open_browser=True):
    """Returns the project panel URL (live LOOP_MONITOR_URL, the work's live MONITOR.json, a live app with the project
    registered, or a new detached app) or None. Reusing never reopens the browser; open_browser=False starts with --no-browser.
    Never raises."""
    try:
        from_environment = monitor_url_from_environment()  # the panel that launched the loop: do not start another one
        if from_environment:
            return from_environment
        reused = running_monitor_url(work)
        if reused:
            return reused
        app = read_live_app()
        if app:
            url = register_project_in_app(app, os.path.abspath(repo))
            if url:
                return url
            print("[loop] warning: the app at %s did not register the project; trying to start another one" % app.get("url"))
        server = server_path or default_server_path()
        if not os.path.isfile(server):
            print("[loop] warning: panel unavailable (%s not found); the loop carries on without it" % server)
            return None
        try:
            os.remove(monitor_file(work))  # stale MONITOR.json (dead pid): the app writes its own when it opens the project
        except OSError:
            pass
        pid, offset = launch_app(os.path.abspath(repo), os.path.abspath(work), server, python or sys.executable, open_browser)
        url = wait_for_monitor(work, pid, offset, wait_seconds)
        if url:
            return url
        if process_is_alive(pid):  # we started it and it did not become valid: leave no orphan process
            PROCESSES.kill_tree(pid)
        print("[loop] warning: the panel did not answer in %.0fs (see %s); the loop carries on without it" % (wait_seconds, loop_state.loop_path(work, "monitor.log")))
        return None
    except Exception as error:  # the panel never breaks the loop
        print("[loop] warning: the panel did not start (%s: %s); the loop carries on without it" % (type(error).__name__, error))
        return None


def is_flag(token, full_name):
    """True for `--answer`, `--answer=x` and abbreviations argparse accepts (`--ans`); at least 4 characters."""
    name = str(token).split("=", 1)[0]
    return len(name) >= 4 and name.startswith("--") and full_name.startswith(name)


def without_answer_flags(argv):
    """argv without --answer/--option (and their values): the relaunch command never repeats the old answer."""
    kept, skip_next = [], False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if is_flag(token, "--answer") or is_flag(token, "--option"):
            skip_next = "=" not in token
            continue
        kept.append(token)
    return kept


def write_loop_command(work, argv, cwd):
    """Writes LOOP-CMD.json: the exact command the panel uses to relaunch the loop (POST /api/answer and /api/resume). Returns the path or None."""
    path = loop_state.loop_path(work, "LOOP-CMD.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"argv": without_answer_flags([str(item) for item in argv]), "cwd": str(cwd)}, handle, ensure_ascii=False, indent=2)
        return path
    except OSError as error:
        print("[loop] warning: LOOP-CMD.json was not written (%s); the panel cannot relaunch the loop" % error)
        return None
