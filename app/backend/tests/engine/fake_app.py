"""Fake app that emulates the contract of `harness.py serve --project <repo>` (used by test_monitor_launch and the infra tests).

Writes app.json in LOOP_DATA_DIR (pid, url, token "stub-token"), answers GET /api/app/health, POST /api/app/projects/open
(token required; notes the path in open.txt next to the script) and GET /p/<id>/api/health, writes MONITOR.json in the
project's work (unless STUB_NO_MONITOR_JSON=1) and prints MONITOR_URL=<project url>. Each start notes its argv in calls.txt.
"""
FAKE_APP_TOKEN = "stub-token"

STUB_APP = r'''
import datetime, hashlib, http.server, json, os, sys, threading
args = sys.argv[1:]
assert args[0] == "serve", args
repo = args[args.index("--project") + 1] if "--project" in args else ""
WRITE_MONITOR_JSON = os.environ.get("STUB_NO_MONITOR_JSON") != "1"
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.txt"), "a", encoding="utf-8") as calls:
    calls.write(" ".join(args) + "\n")
data_dir = os.environ["LOOP_DATA_DIR"]
token = "stub-token"
projects = {}


def work_of(path):
    worktree = os.path.join(os.path.dirname(path), os.path.basename(path) + "-worktree-loop")
    return worktree if os.path.isdir(worktree) else path


def open_project(path):
    path = os.path.abspath(path)
    project_id = hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:10]
    work = work_of(path)
    projects[project_id] = {"repo": path, "work": work}
    if WRITE_MONITOR_JSON:
        folder = os.path.join(work, ".specs", "project", "loop", "json")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "MONITOR.json"), "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "port": port, "url": "http://127.0.0.1:%d/p/%s/" % (port, project_id), "repo": path,
                       "work": work, "started_at": datetime.datetime.now().isoformat(timespec="seconds")}, handle)
    return project_id


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if self.path.startswith("/api/app/health"):
            self.reply(200, {"ok": True, "pid": os.getpid(), "version": "stub", "url": "http://127.0.0.1:%d/" % port})
        elif len(parts) == 4 and parts[0] == "p" and parts[2:] == ["api", "health"] and parts[1] in projects:
            project = projects[parts[1]]
            self.reply(200, {"ok": True, "repo": project["repo"], "work": project["work"], "pid": os.getpid()})
        else:
            self.reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.headers.get("X-Monitor-Token") != token:
            return self.reply(403, {"ok": False, "error": "missing or invalid token"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.startswith("/api/app/projects/open"):
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "open.txt"), "a", encoding="utf-8") as calls:
                calls.write(body["path"] + "\n")
            project_id = open_project(body["path"])
            return self.reply(200, {"ok": True, "project": {"id": project_id}, "url": "/p/%s/" % project_id})
        self.reply(200, {"ok": True})
        if self.path.startswith("/api/app/shutdown"):
            threading.Thread(target=server.shutdown).start()

    def log_message(self, *ignored):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
os.makedirs(data_dir, exist_ok=True)
with open(os.path.join(data_dir, "app.json"), "w", encoding="utf-8") as handle:
    json.dump({"pid": os.getpid(), "port": port, "url": "http://127.0.0.1:%d/" % port, "token": token}, handle)
first_url = "http://127.0.0.1:%d/" % port
if repo:
    first_url += "p/%s/" % open_project(repo)
print("MONITOR_URL=" + first_url, flush=True)
print("APP_URL=http://127.0.0.1:%d/" % port, flush=True)
server.serve_forever()
'''
