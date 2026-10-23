"""Act mode per project (`/p/<id>/api/actions`, `action/propose`, `action/run`). Port of ActionTests, WorktreeActionTests
and WorktreeAsRepoTests of the legacy panel: fake delivery.py/loop_summary.py/claude and REAL temporary git repos."""
import json
import os
import shutil
import subprocess
import time
import unittest

from harness_backend.infrastructure.git_client import DEFAULT_INTEGRATION_BRANCH
from tests.api.support_commands import PROJECT_ID, ROADMAP, CommandsTestCase, Fixture

INTEGRATION_VARIABLES = ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH")
# fake router claude: records argv/stdin in router_record.jsonl and answers according to router_mode.txt
FAKE_ROUTER_CLAUDE = """import json, os, sys
folder = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(folder, "router_mode.txt")
mode = open(path, encoding="utf-8").read().strip() if os.path.exists(path) else "deliver"
stdin = sys.stdin.read()
with open(os.path.join(folder, "router_record.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\\n")
if mode == "error":
    sys.stderr.write("router failed\\n")
    sys.exit(2)
proposal = lambda action: json.dumps({"action": action, "explanation": "Wants to open the PR.", "confidence": "high"})
text = {"deliver": proposal("deliver"), "summary": proposal("summary"), "none": proposal("none"), "outside": proposal("delete-everything"),
        "clean": json.dumps({"action": "clean_worktrees", "explanation": "Delete the worktree.", "confidence": "high", "targets": ["loop/test", "project"]}),
        "push": json.dumps({"action": "push", "explanation": "Push only.", "confidence": "high", "targets": []}),
        "garbage": "hi, how are you?", "bad_json": "{action: deliver", "fence": "```json\\n" + proposal("deliver") + "\\n```"}[mode]
print(json.dumps({"result": text, "total_cost_usd": 0.01, "session_id": "sess-router", "is_error": False}))
"""
# fake delivery.py: records argv/headless/cwd/LOOP_PR_MODE in delivery_record.jsonl; --dry-run answers the preview; without
# it answers output.json (next to it) and exits with the code in output_exit.txt
FAKE_DELIVERY = """import json, os, sys, time
folder = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(folder, "delivery_record.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "headless": "LOOP_HEADLESS" in os.environ, "cwd": os.getcwd(), "pr_mode": os.environ.get("LOOP_PR_MODE")}) + "\\n")
def read(name, default):
    path = os.path.join(folder, name)
    return open(path, encoding="utf-8").read().strip() if os.path.exists(path) else default
if "--dry-run" in sys.argv:
    print(json.dumps({"push": read("dry_push.txt", "dry-run: git push -u origin loop/x"), "pr": "https://github.com/o/r/compare/main...loop/x", "pr_status": "compare link (PR not created)"}, indent=2))
    sys.exit(0)
if read("mode.txt", "ok") == "sleep":
    time.sleep(60)
print(read("output.json", json.dumps({"push": "done: origin/loop/x", "pr": "https://github.com/o/r/pull/7", "pr_status": "PR created"}, indent=2)))
sys.exit(int(read("output_exit.txt", "0")))
"""
FAKE_SUMMARY = """import os, sys
folder = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(folder, "summary_record.txt"), "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
if "--out" in sys.argv:
    destination = sys.argv[sys.argv.index("--out") + 1]
    os.makedirs(destination, exist_ok=True)
    open(os.path.join(destination, "loop-summary-test.md"), "w", encoding="utf-8").write("summary")
print("C:/Users/dev/output/loop-summary-test.md token=abc123secret")
"""


def prepare_fake_scripts(fixture):
    stub = os.path.join(fixture.tmp, "action")
    fixture.write(os.path.join(stub, "fake_delivery.py"), FAKE_DELIVERY)
    fixture.write(os.path.join(stub, "fake_summary.py"), FAKE_SUMMARY)
    fixture.write(os.path.join(stub, "fake_claude.py"), FAKE_ROUTER_CLAUDE)
    fixture.scripts["delivery"] = os.path.join(stub, "fake_delivery.py")
    fixture.scripts["loop_summary"] = os.path.join(stub, "fake_summary.py")
    fixture.claude_binary = os.path.join(stub, "fake_claude.py")
    return stub


def read_json_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def read_json_file(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def set_env(test, **values):
    """Sets (or removes, with None) environment variables for one test and restores them afterwards."""
    for name, value in values.items():
        previous = os.environ.get(name)
        test.addCleanup(lambda name=name, previous=previous: os.environ.pop(name, None) if previous is None else os.environ.__setitem__(name, previous))
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class ActionTests(CommandsTestCase):
    def prepare(self, loop_status="done", branch="loop/x"):
        self.stub = prepare_fake_scripts(self.fixture)
        module = self.fixture.start()
        self.fixture.write_json("meta.json", {"branch": branch, "base": "main", "turns": 3})
        if loop_status:
            self.fixture.set_loop(loop_status)
        return module

    def delivery_records(self):
        return read_json_lines(os.path.join(self.stub, "delivery_record.jsonl"))

    def actions_log(self):
        return read_json_lines(os.path.join(self.fixture.loop, "internal", "actions.log"))

    def run_action(self, **body):
        return self.fixture.call("/api/action/run", "POST", body)

    def actions(self):
        status, data = self.fixture.call("/api/actions")
        self.assertEqual(status, 200)
        return {a["id"]: a for a in data["actions"]}

    def propose(self, text="open the PR to the integration branch and push", mode=None, job_route="/api/chat/job/"):
        if mode:
            self.fixture.write(os.path.join(self.stub, "router_mode.txt"), mode)
        status, sent = self.fixture.call("/api/action/propose", "POST", {"text": text})
        self.assertEqual(status, 202)
        deadline = time.time() + 30
        while time.time() < deadline:
            status, job = self.fixture.call(job_route + sent["job"])
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("the job did not finish")

    # ---- GET actions
    def test_deliver_preview_comes_from_the_dry_run_and_never_pushes(self):
        set_env(self, LOOP_PR_BASE=None, LOOP_INTEGRATION_BRANCH="integration")
        self.prepare()
        actions = self.actions()
        self.assertEqual(list(actions), ["deliver", "summary", "push", "clean_worktrees"])
        deliver = actions["deliver"]
        self.assertEqual((deliver["available"], deliver["unavailable_reason"]), (True, None))
        self.assertEqual((deliver["preview"]["branch"], deliver["preview"]["base"], deliver["preview"]["push"]),
                         ("loop/x", "integration", "dry-run: git push -u origin loop/x"))
        self.assertEqual(deliver["title"], "Deliver: push the branch + PR to integration")
        self.assertIsNone(deliver["preview"]["existing_pr"])
        self.assertEqual(deliver["preview"]["open_features"], ["service-a", "service-b", "service-d"])
        self.assertTrue(actions["summary"]["available"])
        self.assertEqual(actions["summary"]["preview"]["destination"], os.path.abspath(self.fixture.output_dir))
        record, = self.delivery_records()
        self.assertEqual(record["argv"][1:], ["--dry-run"])
        self.assertFalse(record["headless"])

    def test_deliver_preview_base_follows_the_pr_base_alias_and_the_default(self):
        set_env(self, LOOP_PR_BASE=None, LOOP_INTEGRATION_BRANCH=None)
        self.prepare()
        self.assertEqual(self.actions()["deliver"]["preview"]["base"], DEFAULT_INTEGRATION_BRANCH)
        set_env(self, LOOP_PR_BASE="release-train")
        self.assertEqual(self.actions()["deliver"]["preview"]["base"], "release-train")

    def test_existing_pr_shows_up_in_the_deliver_preview(self):
        self.prepare()
        self.fixture.write_json("meta.json", {"branch": "loop/x", "pr": "https://github.com/o/r/pull/3", "pr_status": "PR already existed"})
        self.assertEqual(self.actions()["deliver"]["preview"]["existing_pr"], "https://github.com/o/r/pull/3")

    def test_actions_unavailable_with_a_dry_run_refusal_the_loop_running_or_no_loop(self):
        self.prepare()
        self.fixture.write(os.path.join(self.stub, "dry_push.txt"), "not done: there are still open features (service-a); push only at the end")
        deliver = self.actions()["deliver"]
        self.assertFalse(deliver["available"])
        self.assertIn("open features", deliver["unavailable_reason"])
        self.fixture.set_lock_alive()
        before = len(self.delivery_records())
        actions = self.actions()
        self.assertEqual([a["available"] for a in actions.values()], [False, False, False, False])
        self.assertEqual(actions["summary"]["unavailable_reason"], "the loop is running")
        self.assertEqual(len(self.delivery_records()), before, "with the loop running not even the dry-run runs")
        os.remove(self.fixture.at("LOOP.lock"))
        os.remove(self.fixture.at("LOOP.json"))
        self.assertEqual(self.actions()["summary"]["unavailable_reason"], "there is no loop in this worktree")

    # ---- POST action/run
    def test_run_deliver_runs_the_script_without_headless_writes_meta_and_log(self):
        set_env(self, LOOP_HEADLESS="1")
        self.prepare()
        status, data = self.run_action(action="deliver", confirm=True)
        self.assertEqual(status, 200)
        self.assertEqual((data["ok"], data["pr"], data["pr_status"], data["push"]), (True, "https://github.com/o/r/pull/7", "PR created", "done: origin/loop/x"))
        self.assertIn("done: origin/loop/x", data["output"])
        record, = self.delivery_records()
        self.assertEqual(record["argv"][1:], [])
        self.assertFalse(record["headless"], "LOOP_HEADLESS is never set")
        self.assertEqual(os.path.normcase(os.path.realpath(record["cwd"])), os.path.normcase(os.path.realpath(self.fixture.work)))
        meta = read_json_file(self.fixture.at("meta.json"))
        self.assertEqual((meta["pr"], meta["pr_status"], meta["push"], meta["turns"]), ("https://github.com/o/r/pull/7", "PR created", "done: origin/loop/x", 3))
        self.assertEqual([(line["action"], line["ok"], line["summary"]) for line in self.actions_log()], [("deliver", True, "done: origin/loop/x")])
        self.assertTrue(self.actions_log()[0]["t"])

    def test_refused_deliver_does_not_write_the_pr_and_returns_it_in_push(self):
        self.prepare()
        self.fixture.write(os.path.join(self.stub, "output.json"), json.dumps({"push": "refused: main is a protected branch; feature branch only", "pr": None, "pr_status": "not created"}))
        self.fixture.write(os.path.join(self.stub, "output_exit.txt"), "1")
        status, data = self.run_action(action="deliver", confirm=True)
        self.assertEqual((status, data["ok"]), (200, False))
        self.assertIn("refused", data["push"])
        self.assertNotIn("pr", read_json_file(self.fixture.at("meta.json")))
        self.assertFalse(self.actions_log()[0]["ok"])

    def test_run_summary_redacts_credentials_in_the_output(self):
        self.prepare()
        status, data = self.run_action(action="summary", confirm=True)
        self.assertEqual((status, data["ok"], data["pr"], data["push"]), (200, True, None, None))
        self.assertIn("loop-summary-test.md", data["output"])
        self.assertNotIn("abc123secret", data["output"])
        with open(os.path.join(self.stub, "summary_record.txt"), encoding="utf-8") as handle:
            record = handle.read().split()
        self.assertEqual(os.path.normcase(record[0]), os.path.normcase(self.fixture.work))
        self.assertEqual(record[1:], ["--out", os.path.abspath(self.fixture.output_dir)], "the output folder always goes in --out")
        self.assertEqual(self.delivery_records(), [])

    def test_run_summary_follows_the_configured_output_dir(self):
        module = self.prepare()
        other = os.path.join(self.fixture.tmp, "other-output")
        module.action_service.config.output_dir = other
        self.assertEqual(self.run_action(action="summary", confirm=True)[1]["ok"], True)
        with open(os.path.join(self.stub, "summary_record.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().split()[1:], ["--out", os.path.abspath(other)])
        self.assertEqual(self.actions()["summary"]["preview"]["destination"], os.path.abspath(other))

    def test_run_requires_confirm_true_and_a_whitelisted_action(self):
        self.prepare()
        for body in ({"action": "deliver"}, {"action": "deliver", "confirm": "true"}, {"action": "deliver", "confirm": 1}, {"action": "deliver", "confirm": False}):
            self.assertEqual(self.run_action(**body)[0], 400, body)
        for action in ("push", "rm -rf", "delete", "", None, "DELIVER", ["deliver"]):
            status, data = self.run_action(action=action, confirm=True)
            self.assertEqual(status, 400, action)
        self.assertEqual(self.delivery_records(), [])
        self.assertFalse(os.path.exists(os.path.join(self.stub, "summary_record.txt")))
        self.assertEqual(self.fixture.call("/api/action/run", "POST", {"action": "deliver", "confirm": True}, token=None)[0], 403)
        self.assertEqual(self.delivery_records(), [])

    def test_run_is_409_with_the_loop_running_a_live_lock_or_no_loop(self):
        module = self.prepare()
        self.fixture.set_lock_alive()
        self.assertEqual(self.run_action(action="deliver", confirm=True)[0], 409)
        self.assertEqual(self.run_action(action="summary", confirm=True)[0], 409)
        os.remove(self.fixture.at("LOOP.lock"))
        module.loop_control_service.relaunched_pid_by_project[PROJECT_ID] = os.getpid()  # a loop launched from here and alive
        self.assertEqual(self.run_action(action="summary", confirm=True)[0], 409)
        module.loop_control_service.relaunched_pid_by_project[PROJECT_ID] = 0
        os.remove(self.fixture.at("LOOP.json"))
        status, data = self.run_action(action="summary", confirm=True)
        self.assertEqual(status, 409)
        self.assertIn("there is no loop", data["error"])
        self.assertEqual(self.delivery_records(), [])
        self.assertFalse(os.path.exists(os.path.join(self.stub, "summary_record.txt")))

    def test_run_timeout_kills_the_process(self):
        module = self.prepare()
        self.fixture.write(os.path.join(self.stub, "mode.txt"), "sleep")
        module.action_service.action_timeout_seconds = 1.5
        status, data = self.run_action(action="deliver", confirm=True)
        self.assertEqual((status, data["ok"]), (200, False))
        self.assertIn("timed out", data["output"])
        self.assertNotIn("pr", read_json_file(self.fixture.at("meta.json")))

    # ---- POST action/propose (router)
    def test_propose_router_argv_haiku_no_tools_no_mcp_text_through_stdin(self):
        self.prepare()
        job = self.propose("open the PR and push SECRET-IN-THE-REQUEST")
        self.assertEqual(job["state"], "ok")
        self.assertEqual(job["proposal"], {"action": "deliver", "explanation": "Wants to open the PR.", "confidence": "high"})
        self.assertEqual((job["reply"], job["cost_usd"]), ("Wants to open the PR.", 0.01))
        record = read_json_lines(os.path.join(self.stub, "router_record.jsonl"))[0]
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.2")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1:argv.index("--setting-sources")],
                         ["Read", "Grep", "Glob", "Bash", "Edit", "Write", "NotebookEdit"])
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")  # runtime isolation (default on) sits between the tools and the MCP flags
        self.assertLess(argv.index("--settings"), argv.index("--strict-mcp-config"))
        self.assertEqual(argv[argv.index("--strict-mcp-config") + 1:], ["--mcp-config", '{"mcpServers":{}}'])
        self.assertEqual(argv[:4], ["-p", "--output-format", "stream-json", "--verbose"])
        self.assertNotIn("--resume", argv)
        self.assertFalse(any("SECRET-IN-THE-REQUEST" in item for item in argv))
        self.assertIn("SECRET-IN-THE-REQUEST", record["stdin"])
        self.assertTrue(record["stdin"].startswith("You are a router."))
        self.assertIn("deliver|summary|push|clean_worktrees|none", record["stdin"])
        self.assertEqual(self.delivery_records(), [])
        self.assertEqual(self.propose(job_route="/api/action/job/")["proposal"]["action"], "deliver", "action/job alias")

    def test_propose_an_invalid_reply_or_one_outside_the_whitelist_becomes_none_and_nothing_runs(self):
        self.prepare()
        cases = {"summary": "summary", "outside": "none", "garbage": "none", "bad_json": "none", "fence": "deliver", "none": "none"}
        for mode, expected in cases.items():
            job = self.propose(mode=mode)
            self.assertEqual(job["state"], "ok", mode)
            self.assertEqual(job["proposal"]["action"], expected, mode)
            self.assertIn(job["proposal"]["confidence"], ("high", "low"))
        self.assertEqual(self.delivery_records(), [], "the router never runs anything")
        self.assertFalse(os.path.exists(os.path.join(self.stub, "summary_record.txt")))
        self.assertEqual(self.actions_log(), [])

    def test_propose_claude_error_and_validations(self):
        self.prepare()
        job = self.propose(mode="error")
        self.assertEqual((job["state"], job["proposal"]), ("error", None))
        self.assertIn("router failed", job["error"])
        self.assertEqual(self.fixture.call("/api/action/propose", "POST", {"text": "  "})[0], 400)
        self.assertEqual(self.fixture.call("/api/action/propose", "POST", {"text": "x" * 2001})[0], 413)
        self.assertEqual(self.fixture.call("/api/action/propose", "POST", {"text": "x"}, token=None)[0], 403)
        self.assertEqual(self.fixture.call("/api/action/job/j-missing")[0], 404)


# ---------------------------------------------------------------- worktree and push actions (REAL temporary git repos)
def git_run(cwd, *args):
    done = subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-C", cwd] + list(args), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), done.stderr))
    return done.stdout.strip()


class WorktreeActionTests(CommandsTestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.stop)
        fx = self.fixture
        shutil.rmtree(fx.work)
        git_run(fx.repo, "init", "-b", "main")
        fx.write(os.path.join(fx.repo, "README.md"), "base\n")
        git_run(fx.repo, "add", "-A")
        git_run(fx.repo, "commit", "-m", "base")
        fx.write(os.path.join(fx.repo, ".git", "info", "exclude"), ".specs/\n")  # .specs does not dirty the worktree
        git_run(fx.repo, "worktree", "add", "-b", "loop/test", fx.work)
        fx.write(os.path.join(fx.project_dir, "ROADMAP.md"), ROADMAP)
        fx.write(os.path.join(fx.work, ".specs", "features", "f1", "spec.md"), "# f1\n")
        fx.write(os.path.join(fx.work, ".specs", "node_modules", "junk.js"), "x")  # stays out of the backup
        fx.write_json("meta.json", {"branch": "loop/test", "base": "main"})
        fx.set_loop("done")
        self.output_dir = fx.output_dir
        self.stub = prepare_fake_scripts(fx)
        self.module = fx.start()

    def worktrees(self):
        actions = {a["id"]: a for a in self.fixture.call("/api/actions")[1]["actions"]}
        return actions["clean_worktrees"]["preview"]["worktrees"], actions

    def clean(self, **body):
        return self.fixture.call("/api/action/run", "POST", dict({"action": "clean_worktrees", "confirm": True}, **body))

    def branches(self):
        return git_run(self.fixture.repo, "branch", "--list", "loop/*")

    def loop_status(self):
        context = self.fixture.project_service.get_project_context(PROJECT_ID)
        return self.module.action_service.loop_status_service.get_loop_summary(context)["status"]

    def test_preview_lists_only_sibling_loop_worktrees_with_dirty_unpushed_and_specs(self):
        fx = self.fixture
        git_run(fx.repo, "worktree", "add", "-b", "feature/x", os.path.join(fx.tmp, "project-worktree-other"))  # branch outside loop/*
        os.makedirs(os.path.join(fx.tmp, "nested"))
        git_run(fx.repo, "worktree", "add", "-b", "loop/outside", os.path.join(fx.tmp, "nested", "project-worktree-loop-2"))  # not a sibling
        git_run(fx.repo, "worktree", "add", "-b", "loop/nomark", os.path.join(fx.tmp, "project-other-folder"))  # name without -worktree-loop
        items, actions = self.worktrees()
        self.assertEqual([w["path"] for w in items], [os.path.normpath(fx.work)])
        item = items[0]
        self.assertEqual((item["branch"], item["dirty"], item["unpushed_commits"], item["has_specs"]), ("loop/test", False, 0, True))
        self.assertTrue(actions["push"]["available"])
        self.assertEqual(actions["push"]["preview"]["worktrees"], items)
        self.assertEqual(actions["clean_worktrees"]["preview"]["backup_dir"], os.path.abspath(self.output_dir))
        self.assertNotIn(os.path.normpath(fx.repo), [w["path"] for w in items], "the main repo never shows up")
        fx.write(os.path.join(fx.work, "new.txt"), "dirty")
        fx.write(os.path.join(fx.work, "README.md"), "changed\n")
        git_run(fx.work, "add", "README.md")
        git_run(fx.work, "commit", "-m", "extra")
        item = self.worktrees()[0][0]
        self.assertEqual((item["dirty"], item["unpushed_commits"]), (True, 1))

    def test_preview_with_an_upstream_counts_against_the_upstream(self):
        fx = self.fixture
        git_run(fx.repo, "remote", "add", "origin", "https://example.invalid/r.git")
        git_run(fx.work, "update-ref", "refs/remotes/origin/loop/test", "HEAD")
        git_run(fx.work, "config", "branch.loop/test.remote", "origin")
        git_run(fx.work, "config", "branch.loop/test.merge", "refs/heads/loop/test")
        for name in ("a", "b"):
            fx.write(os.path.join(fx.work, name + ".txt"), name)
            git_run(fx.work, "add", name + ".txt")
            git_run(fx.work, "commit", "-m", name)
        self.assertEqual(self.worktrees()[0][0]["unpushed_commits"], 2)

    def test_actions_unavailable_without_a_loop_worktree_or_while_running(self):
        self.fixture.set_lock_alive()
        actions = self.worktrees()[1]
        self.assertEqual((actions["push"]["available"], actions["clean_worktrees"]["unavailable_reason"]), (False, "the loop is running"))
        os.remove(self.fixture.at("LOOP.lock"))
        git_run(self.fixture.repo, "worktree", "remove", "--force", self.fixture.work)
        actions = self.worktrees()[1]
        self.assertEqual(actions["push"]["unavailable_reason"], "there is no loop worktree")

    def test_clean_makes_a_verified_backup_removes_the_folder_keeps_the_branch_and_the_status_becomes_no_loop(self):
        fx = self.fixture
        status, data = self.clean(targets=[fx.work])
        self.assertEqual(status, 200, data)
        result, = data["results"]
        self.assertEqual((data["ok"], result["deleted"], result["error"]), (True, True, None))
        backup = result["backup"]
        self.assertEqual(os.path.dirname(backup), os.path.abspath(self.output_dir))
        self.assertRegex(os.path.basename(backup), r"^loop-project-\d{4}-\d\d-\d\d-\d{4}$")
        self.assertTrue(os.path.isfile(os.path.join(backup, "specs", "features", "f1", "spec.md")))
        self.assertTrue(os.path.isfile(os.path.join(backup, "specs", "project", "ROADMAP.md")))
        self.assertFalse(os.path.exists(os.path.join(backup, "specs", "node_modules")), "node_modules stays out of the backup")
        self.assertTrue(any(name.endswith(".md") for name in os.listdir(backup)), "summary generated")
        self.assertFalse(os.path.exists(fx.work))
        self.assertTrue(os.path.isdir(fx.repo), "the main repo stays")
        self.assertIn("loop/test", self.branches(), "the branch is not deleted")
        self.assertNotIn(fx.work.replace("\\", "/"), git_run(fx.repo, "worktree", "list", "--porcelain").replace("\\", "/"))
        self.assertEqual(self.loop_status(), "no_loop")
        self.assertFalse(os.path.exists(fx.work), "writing actions.log does not recreate the deleted worktree")
        self.assertEqual(self.fixture.call("/api/actions")[0], 200, "the app keeps answering")

    def test_backup_with_a_failed_summary_aborts_without_deleting(self):
        fx = self.fixture
        fx.write(os.path.join(self.stub, "fake_summary.py"), "import sys\nsys.stderr.write('it broke')\nsys.exit(4)\n")
        status, data = self.clean(targets=[fx.work])
        result, = data["results"]
        self.assertEqual((status, data["ok"], result["deleted"]), (200, False, False))
        self.assertIn("backup failed", result["error"])
        self.assertIn("nothing was deleted", result["error"])
        self.assertTrue(os.path.isdir(fx.work))
        self.assertIn(os.path.normpath(fx.work), [w["path"] for w in self.worktrees()[0]])

    def test_backup_in_an_invalid_output_dir_aborts_without_deleting(self):
        fx = self.fixture
        fx.write(os.path.join(fx.tmp, "file-in-place-of-the-folder"), "x")
        fx.config.output_dir = os.path.join(fx.tmp, "file-in-place-of-the-folder")
        status, data = self.clean(targets=[fx.work])
        self.assertFalse(data["results"][0]["deleted"])
        self.assertIn("backup failed", data["results"][0]["error"])
        self.assertTrue(os.path.isdir(fx.work))

    def test_backup_with_a_copy_that_does_not_match_aborts_without_deleting(self):
        fx = self.fixture
        original = shutil.copytree

        def incomplete_copy(source, destination, *args, **kwargs):
            original(source, destination, *args, **kwargs)
            if os.path.basename(destination) == "specs":  # copytree calls itself on subfolders: only touch the root
                os.remove(os.path.join(destination, "project", "ROADMAP.md"))
            return destination

        shutil.copytree = incomplete_copy
        self.addCleanup(setattr, shutil, "copytree", original)
        status, data = self.clean(targets=[fx.work])
        self.assertIn("does not match", data["results"][0]["error"])
        self.assertFalse(data["results"][0]["deleted"])
        self.assertTrue(os.path.isdir(fx.work))

    def test_dirty_is_refused_without_force_and_accepted_with_force(self):
        fx = self.fixture
        fx.write(os.path.join(fx.work, "uncommitted.txt"), "important")
        status, data = self.clean(targets=[fx.work])
        result = data["results"][0]
        self.assertEqual((status, result["deleted"], result["backup"]), (200, False, None))
        self.assertIn("force", result["error"])
        self.assertTrue(os.path.isfile(os.path.join(fx.work, "uncommitted.txt")))
        self.assertEqual(self.clean(targets=[fx.work], force="yes")[0], 400)
        status, data = self.clean(targets=[fx.work], force=True)
        self.assertEqual((status, data["ok"], data["results"][0]["deleted"]), (200, True, True))
        self.assertFalse(os.path.exists(fx.work))
        self.assertIn("loop/test", self.branches())

    def test_target_outside_the_preview_is_400_and_nothing_is_deleted(self):
        fx = self.fixture
        other = os.path.join(fx.tmp, "project-worktree-other")
        git_run(fx.repo, "worktree", "add", "-b", "feature/y", other)
        for target in (fx.repo, other, os.path.join(fx.tmp, "anything"), os.path.join(fx.work, ".."), fx.tmp):
            status, data = self.clean(targets=[fx.work, target])
            self.assertEqual(status, 400, target)
            self.assertIn("outside the list", data["error"])
        self.assertTrue(os.path.isdir(fx.work), "an invalid target aborts the whole request, including the valid one")
        self.assertTrue(os.path.isdir(fx.repo) and os.path.isdir(other))
        self.assertFalse(os.path.exists(self.output_dir), "not even a backup was made")

    def test_clean_requires_targets_and_confirm_and_does_not_run_with_the_loop_running(self):
        fx = self.fixture
        self.assertEqual(self.clean()[0], 400)
        self.assertEqual(self.clean(targets=[])[0], 400)
        self.assertEqual(self.clean(targets="x")[0], 400)
        self.assertEqual(self.fixture.call("/api/action/run", "POST", {"action": "clean_worktrees", "targets": [fx.work]})[0], 400)
        self.assertEqual(self.fixture.call("/api/action/run", "POST", {"action": "clean_worktrees", "confirm": True, "targets": [fx.work]}, token=None)[0], 403)
        fx.set_lock_alive()
        self.assertEqual(self.clean(targets=[fx.work])[0], 409)
        self.assertTrue(os.path.isdir(fx.work))

    def test_push_uses_delivery_with_pr_mode_off_per_target_and_defaults_to_all(self):
        fx = self.fixture
        second = os.path.join(fx.tmp, "project-worktree-loop-2")
        git_run(fx.repo, "worktree", "add", "-b", "loop/second", second)
        status, data = fx.call("/api/action/run", "POST", {"action": "push", "confirm": True})
        self.assertEqual((status, data["ok"]), (200, True))
        self.assertEqual(sorted(r["branch"] for r in data["results"]), ["loop/second", "loop/test"])
        self.assertEqual(data["results"][0]["push"], "done: origin/loop/x")
        self.assertEqual(set(data["results"][0]), {"path", "branch", "ok", "push", "output"})
        records = read_json_lines(os.path.join(self.stub, "delivery_record.jsonl"))
        self.assertEqual(sorted(os.path.normcase(r["argv"][0]) for r in records), sorted(os.path.normcase(os.path.normpath(p)) for p in (fx.work, second)))
        self.assertTrue(all(r["argv"][1:] == [] and r["pr_mode"] == "off" and not r["headless"] for r in records))
        os.remove(os.path.join(self.stub, "delivery_record.jsonl"))
        status, data = fx.call("/api/action/run", "POST", {"action": "push", "confirm": True, "targets": [second]})
        self.assertEqual([r["branch"] for r in data["results"]], ["loop/second"])
        self.assertEqual(len(read_json_lines(os.path.join(self.stub, "delivery_record.jsonl"))), 1)
        self.assertEqual(fx.call("/api/action/run", "POST", {"action": "push", "confirm": True, "targets": [fx.repo]})[0], 400)
        fx.write(os.path.join(self.stub, "output.json"), json.dumps({"push": "refused: protected branch", "pr": None, "pr_status": "not created"}))
        fx.write(os.path.join(self.stub, "output_exit.txt"), "1")
        status, data = fx.call("/api/action/run", "POST", {"action": "push", "confirm": True, "targets": [second]})
        self.assertEqual((status, data["ok"], data["results"][0]["push"]), (200, False, "refused: protected branch"))

    def wait_for_proposal(self, job_id):
        deadline = time.time() + 30
        job = {"state": "running"}
        while job["state"] == "running" and time.time() < deadline:
            job = self.fixture.call("/api/chat/job/" + job_id)[1]
            time.sleep(0.1)
        return job

    def test_router_recognizes_push_and_clean_and_only_returns_preview_targets(self):
        fx = self.fixture
        fx.write(os.path.join(self.stub, "router_mode.txt"), "clean")
        status, sent = fx.call("/api/action/propose", "POST", {"text": "delete the loop/test worktree and the main repo one"})
        self.assertEqual(status, 202)
        job = self.wait_for_proposal(sent["job"])
        self.assertEqual(job["proposal"]["action"], "clean_worktrees")
        self.assertEqual(job["proposal"]["targets"], [os.path.normpath(fx.work)])  # the cited main repo is dropped
        self.assertTrue(os.path.isdir(fx.work), "propose never runs")
        fx.write(os.path.join(self.stub, "router_mode.txt"), "push")
        job = self.wait_for_proposal(fx.call("/api/action/propose", "POST", {"text": "just push"})[1]["job"])
        self.assertEqual((job["proposal"]["action"], job["proposal"]["targets"]), ("push", []))
        self.assertFalse(os.path.exists(os.path.join(self.stub, "delivery_record.jsonl")))


class WorktreeAsRepoTests(WorktreeActionTests):
    """Same scenarios, with the project opened on the loop worktree ITSELF (repo_path = worktree)."""

    def restart(self, repo_path):
        self.fixture.stop_server()
        self.module = self.fixture.start(repo=repo_path)
        return self.module

    def setUp(self):
        super().setUp()
        self.restart(self.fixture.work)

    def test_repo_equal_to_the_worktree_and_repo_equal_to_the_main_give_the_same_preview(self):
        fx = self.fixture
        git_run(fx.repo, "worktree", "add", "-b", "loop/second", os.path.join(fx.tmp, "project-worktree-loop-2"))
        from_worktree = self.worktrees()[0]
        self.assertEqual(sorted(w["branch"] for w in from_worktree), ["loop/second", "loop/test"])
        self.assertIn(os.path.normpath(fx.work), [w["path"] for w in from_worktree], "the project's own work is a valid target")
        self.assertNotIn(os.path.normpath(fx.repo), [w["path"] for w in from_worktree])
        self.restart(fx.repo)
        self.assertEqual(self.worktrees()[0], from_worktree)

    def test_deliver_and_summary_keep_working_with_repo_equal_to_the_worktree(self):
        fx = self.fixture
        fx.set_loop("done")
        actions = self.worktrees()[1]
        self.assertTrue(actions["deliver"]["available"], actions["deliver"])
        self.assertTrue(actions["summary"]["available"])
        self.assertEqual(fx.call("/api/action/run", "POST", {"action": "deliver", "confirm": True})[1]["ok"], True)
        self.assertEqual(fx.call("/api/action/run", "POST", {"action": "summary", "confirm": True})[1]["ok"], True)

    def test_cleaning_the_own_work_with_repo_equal_to_the_worktree_does_not_delete_the_main(self):
        fx = self.fixture
        status, data = self.clean(targets=[fx.work])
        self.assertEqual((status, data["ok"], data["results"][0]["deleted"]), (200, True, True))
        self.assertTrue(os.path.isfile(os.path.join(data["results"][0]["backup"], "specs", "features", "f1", "spec.md")))
        self.assertFalse(os.path.exists(fx.work))
        self.assertTrue(os.path.isdir(fx.repo))
        self.assertIn("loop/test", self.branches())
        self.assertEqual(self.clean(targets=[fx.repo])[0], 400)


if __name__ == "__main__":
    unittest.main()
