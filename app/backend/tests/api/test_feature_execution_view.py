"""Feature execution view (FEV-01..05): `SCOPE_TASK` parsed from the subagent prompt, `task` on the agent rows, waves with `split_by_limit`,
and `GET /feature/{slug}` (header, steps, tasks with their executors, agents, live matching of the running session)."""
import os
import shutil
import tempfile
import time
import unittest

from harness_backend.repositories.claude_transcript_repository import PROMPT_TASK_PATTERN
from harness_backend.repositories.task_status_repository import TaskStatusRepository

from .support_read import ReadTestCase, TranscriptFixtureMixin, assistant_row, shifted, tool_use, user_row

import task_waves  # noqa: E402  (the engine folder is on sys.path once the app package is imported)

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
LIVE_SESSION_ID = "99999999-8888-7777-6666-555555555555"
TASKS_MARKDOWN = ("### T1: Model [P]\n**Type**: impl\n**Files**: src/a/model.py\n**Deps**: none\n**Done when**: ok\n\n"
                  "### T2: Service [P]\n**Type**: impl\n**Files**: src/a/service.py\n**Deps**: none\n**Done when**: ok\n\n"
                  "### T3: Controller [P]\n**Type**: impl\n**Files**: src/a/controller.py\n**Deps**: none\n**Done when**: ok\n\n"
                  "### T4: Wire it\n**Type**: wiring\n**Files**: src/a/wiring.py\n**Deps**: T3\n**Done when**: ok\n")
START = "2026-10-15T15:00:00.000Z"


def build_task(task_id, files, deps="none"):
    return "### %s: Task %s\n**Type**: impl\n**Files**: %s\n**Deps**: %s\n**Done when**: ok\n\n" % (task_id, task_id, files, deps)


def build_final_agent_rows(prompt, final_message, start):
    """Prompt, one tool call and the final return through SubagentHandback."""
    return [user_row(start, prompt),
            assistant_row(shifted(start, 1), "msg_1", [tool_use("tu_1", "Read", file_path="C:/x/spec.md")], {"input_tokens": 5, "output_tokens": 5}),
            assistant_row(shifted(start, 30), "msg_2", [tool_use("tu_2", "SubagentHandback", message=final_message)], {"input_tokens": 1, "output_tokens": 1},
                          stop="tool_use")]


def build_unfinished_agent_rows(prompt, start):
    """A subagent still working: no final return and no end_turn."""
    return [user_row(start, prompt),
            assistant_row(shifted(start, 1), "msg_1", [tool_use("tu_1", "Read", file_path="C:/x/spec.md")], {"input_tokens": 5, "output_tokens": 5})]


class TaskPlanTests(unittest.TestCase):
    """FEV-01 and FEV-02 on the repository: deps/files/parallel, waves and `split_by_limit`."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fev-repo-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.repository = TaskStatusRepository()

    def write_plan(self, tasks_text, max_parallel):
        roadmap = os.path.join(self.work, ".specs", "project", "ROADMAP.md")
        os.makedirs(os.path.dirname(roadmap), exist_ok=True)
        with open(roadmap, "w", encoding="utf-8") as handle:
            handle.write("# Roadmap\n\n**Max parallel tasks:** %d\n" % max_parallel)
        folder = os.path.join(self.work, ".specs", "features", "feat")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "tasks.md"), "w", encoding="utf-8") as handle:
            handle.write(tasks_text)

    def test_tasks_carry_deps_files_and_the_parallel_mark(self):
        self.write_plan(TASKS_MARKDOWN, 3)
        tasks = {task["id"]: task for task in self.repository.read_feature_tasks(self.work, "feat")["tasks"]}
        self.assertEqual((tasks["T1"]["deps"], tasks["T1"]["files"], tasks["T1"]["parallel"]), ([], ["src/a/model.py"], True))
        self.assertEqual((tasks["T4"]["deps"], tasks["T4"]["files"], tasks["T4"]["parallel"]), (["T3"], ["src/a/wiring.py"], False))

    def test_the_limit_that_pushes_a_task_to_the_next_wave_marks_split_by_limit(self):
        self.write_plan(TASKS_MARKDOWN, 2)
        block = self.repository.read_feature_tasks(self.work, "feat")
        self.assertEqual(block["source"], "tasks.md")
        self.assertEqual(block["waves"], [{"n": 1, "task_ids": ["T1", "T2"], "split_by_limit": True},
                                          {"n": 2, "task_ids": ["T3"], "split_by_limit": False},
                                          {"n": 3, "task_ids": ["T4"], "split_by_limit": False}])

    def test_a_plan_that_fits_the_limit_has_no_split(self):
        self.write_plan(TASKS_MARKDOWN, 3)
        waves = self.repository.read_feature_tasks(self.work, "feat")["waves"]
        self.assertEqual([wave["split_by_limit"] for wave in waves], [False, False])
        self.assertEqual([wave["task_ids"] for wave in waves], [["T1", "T2", "T3"], ["T4"]])

    def test_a_file_conflict_is_not_a_split_by_the_limit(self):
        self.write_plan(build_task("T1", "same.py") + build_task("T2", "same.py"), 3)
        waves = self.repository.read_feature_tasks(self.work, "feat")["waves"]
        self.assertEqual([(wave["task_ids"], wave["split_by_limit"]) for wave in waves], [(["T1"], False), (["T2"], False)])

    def test_a_dependency_is_not_a_split_by_the_limit(self):
        self.write_plan(build_task("T1", "a.py") + build_task("T2", "b.py", "T1"), 1)
        waves = self.repository.read_feature_tasks(self.work, "feat")["waves"]
        self.assertEqual([wave["split_by_limit"] for wave in waves], [False, False])

    def test_a_plan_with_a_problem_has_no_waves_but_keeps_the_tasks(self):
        self.write_plan(build_task("T1", "a.py", "T9"), 3)
        block = self.repository.read_feature_tasks(self.work, "feat")
        self.assertEqual(block["waves"], [])
        self.assertEqual([task["id"] for task in block["tasks"]], ["T1"])

    def test_no_tasks_md_is_source_none(self):
        self.write_plan("", 3)
        self.assertEqual(self.repository.read_feature_tasks(self.work, "feat"),
                         {"source": "none", "max_parallel": 3, "tasks": [], "waves": []})


class ScopeTaskPromptTests(unittest.TestCase):
    def test_the_task_id_is_read_from_the_scope_task_line(self):
        self.assertEqual(PROMPT_TASK_PATTERN.search("FEATURE: feat-x\nSCOPE_TASK: feat-x/T5\nmore").group(1), "T5")
        self.assertEqual(PROMPT_TASK_PATTERN.search("scope_task: a.b-c/t12").group(1), "t12")

    def test_a_prompt_without_the_line_has_no_task(self):
        self.assertIsNone(PROMPT_TASK_PATTERN.search("FEATURE: feat-x\nSCOPE_FEATURE: feat-x"))
        self.assertIsNone(PROMPT_TASK_PATTERN.search("SCOPE_TASK: feat-x"))


class FeatureExecutionViewTests(TranscriptFixtureMixin, ReadTestCase):
    def write_tasks(self, slug="service-a", text=TASKS_MARKDOWN):
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "features", slug, "tasks.md"), text)

    def setup_session(self, subagents, session=SESSION_ID, number=3):
        """A finished turn `number` with `subagents` = [(agent id, role, rows)]."""
        self.setup_projects(session=session, number=number)
        folder = os.path.join(self.slug_dir, session, "subagents")
        for agent_id, role, rows in subagents:
            self.write_lines(os.path.join(folder, "agent-%s.jsonl" % agent_id), rows)
            self.fixture.write(os.path.join(folder, "agent-%s.meta.json" % agent_id), '{"agentType": "%s"}' % role)
        return folder

    def detail(self, slug="service-a", expected=200):
        status, data = self.fixture.call("/api/feature/%s" % slug)
        self.assertEqual(status, expected, data)
        return data

    def executor(self, agent_id, task, start, final="STATUS: DONE\nCHANGED:\n- a"):
        prompt = "FEATURE: service-a\nSCOPE_FEATURE: service-a\nSCOPE_TASK: service-a/%s\nImplement." % task
        return (agent_id, "executor-back", build_final_agent_rows(prompt, final, start))

    def test_the_history_row_of_a_subagent_carries_its_task(self):
        self.setup_session([self.executor("ex1", "T1", START),
                            ("wr1", "spec-writer", build_final_agent_rows("FEATURE: service-a\nWrite the spec.", "STATUS: DONE", START))])
        self.fixture.start(projects_dir=self.projects)
        rows = {agent["id"]: agent for agent in self.history()["turns"][0]["agents"]}
        self.assertEqual((rows["v003:ex1"]["task"], rows["v003:ex1"]["feature"]), ("T1", "service-a"))
        self.assertIsNone(rows["v003:wr1"]["task"])

    def test_an_unknown_slug_is_404(self):
        self.fixture.start()
        status, data = self.fixture.call("/api/feature/does-not-exist")
        self.assertEqual((status, data["ok"]), (404, False))

    def test_an_unknown_project_is_404(self):
        self.fixture.start()
        status, _ = self.fixture.call("/api/feature/service-a", project_id="p-missing")
        self.assertEqual(status, 404)

    def test_the_header_comes_from_the_roadmap(self):
        self.fixture.start()
        data = self.detail()
        self.assertEqual((data["id"], data["status"], data["stage"], data["kind"], data["paths"]), ("service-a", "building", 2, "back", ["src/a"]))
        self.assertEqual(data["deps"], ["base"])  # `dir:` dependencies are not feature dependencies
        self.assertEqual(data["wave"], 2)
        self.assertEqual(data["scope"], "Medium")
        self.assertIsNone(data["claude"])  # no turn yet

    def test_a_feature_without_tasks_md_has_source_none_and_empty_lists(self):
        self.fixture.start()
        data = self.detail()
        self.assertEqual((data["tasks"]["source"], data["tasks"]["tasks"], data["tasks"]["waves"]), ("none", [], []))
        self.assertEqual(data["agents"], [])

    def test_tasks_list_deps_files_waves_and_the_status_file(self):
        self.write_tasks()
        task_waves.write_status(self.fixture.work, "service-a", "T1", "done")
        task_waves.write_status(self.fixture.work, "service-a", "T2", "running", "halfway")
        self.fixture.start()
        block = self.detail()["tasks"]
        self.assertEqual((block["source"], block["max_parallel"]), ("tasks.md", 3))
        self.assertEqual(block["waves"], [{"n": 1, "task_ids": ["T1", "T2", "T3"], "split_by_limit": False},
                                          {"n": 2, "task_ids": ["T4"], "split_by_limit": False}])
        tasks = {task["id"]: task for task in block["tasks"]}
        self.assertEqual((tasks["T1"]["status"], tasks["T2"]["status"], tasks["T2"]["note"], tasks["T4"]["deps"]), ("done", "running", "halfway", ["T3"]))
        self.assertEqual(tasks["T1"]["executors"], [])

    def test_executors_are_matched_by_feature_and_task_oldest_first_with_every_attempt(self):
        self.write_tasks()
        other_feature = ("other", "executor-back", build_final_agent_rows("FEATURE: service-b\nSCOPE_TASK: service-b/T1\nx", "STATUS: DONE", START))
        self.setup_session([self.executor("late", "T1", shifted(START, 600), "STATUS: DONE"),
                            self.executor("first", "T1", START, "STATUS: FAIL\nNOTES: broke"),
                            self.executor("two", "T2", shifted(START, 5)), other_feature])
        self.fixture.start(projects_dir=self.projects)
        data = self.detail()
        tasks = {task["id"]: task for task in data["tasks"]["tasks"]}
        first_task_executors = tasks["T1"]["executors"]
        self.assertEqual([executor["agent_id"] for executor in first_task_executors], ["v003:first", "v003:late"])
        self.assertEqual((first_task_executors[0]["role"], first_task_executors[0]["turn"], first_task_executors[0]["state"],
                          first_task_executors[0]["return_status"]), ("executor-back", 3, "done", "FAIL"))
        self.assertEqual(first_task_executors[1]["return_status"], "DONE")
        self.assertEqual([executor["agent_id"] for executor in tasks["T2"]["executors"]], ["v003:two"])
        self.assertEqual(tasks["T3"]["executors"], [])
        self.assertEqual({call["agent_id"] for call in data["agents"]}, {"v003:first", "v003:late", "v003:two"})  # the other feature is left out
        self.assertEqual([call["task"] for call in data["agents"]], ["T1", "T2", "T1"])

    def test_an_executor_cut_off_with_its_turn_is_interrupted_not_done(self):
        self.write_tasks()
        task_waves.write_status(self.fixture.work, "service-a", "T1", "running")
        task_waves.write_status(self.fixture.work, "service-a", "T2", "done")
        cut_off = ("cut", "executor-back", build_unfinished_agent_rows("FEATURE: service-a\nSCOPE_TASK: service-a/T1\nImplement.", START))
        self.setup_session([cut_off, self.executor("two", "T2", shifted(START, 5))])
        self.fixture.start(projects_dir=self.projects)
        tasks = {task["id"]: task for task in self.detail()["tasks"]["tasks"]}
        self.assertEqual(tasks["T1"]["executors"][0]["state"], "interrupted")
        self.assertEqual(tasks["T2"]["executors"][0]["state"], "done", "a task that finished keeps its executor as it was")

    def test_the_agents_list_has_the_contract_fields_and_the_last_turn_claude(self):
        self.write_tasks()
        self.setup_session([self.executor("ex1", "T1", START)])
        self.fixture.write_json("turns/003.json", {"session_id": SESSION_ID, "claude": "cc", "claude_account": "token, cc account claude"})
        self.fixture.start(projects_dir=self.projects)
        data = self.detail()
        self.assertEqual(data["claude"], {"command": "cc", "account": "token, cc account claude", "auth_method": "token", "auth_email": "",
                                          "auth_plan": "", "cc_account": "claude"})
        call = data["agents"][0]
        self.assertEqual(sorted(call), sorted(["agent_id", "role", "turn", "task", "state", "return_status", "duration_s", "tokens", "started_at",
                                               "finished_at", "summary", "claude", "claude_account", "auth_method", "auth_email", "auth_plan",
                                               "cc_account", "usage_windows", "limit_status"]))  # the last eight: command, sign-in and usage inherited from the turn
        self.assertEqual((call["duration_s"], call["started_at"]), (30, START))

    def test_steps_follow_the_roadmap_status(self):
        self.fixture.start()
        building = self.detail()
        self.assertEqual(building["current_step"], "build")
        self.assertEqual([(step["step"], step["state"]) for step in building["steps"]],
                         [("spec", "done"), ("gate", "done"), ("build", "running"), ("verify", "pending"), ("commit", "pending")])
        verified = self.detail("base")
        self.assertEqual((verified["current_step"], {step["state"] for step in verified["steps"]}), ("commit", {"done"}))
        approved = self.detail("service-d")
        self.assertEqual([step["state"] for step in approved["steps"]], ["done", "done", "pending", "pending", "pending"])
        pending = self.detail("service-b")
        self.assertEqual((pending["current_step"], pending["steps"][0]["state"]), ("spec", "pending"))

    def test_a_blocked_feature_fails_in_the_step_of_its_last_agent(self):
        self.setup_session([("ex1", "executor-back", build_final_agent_rows("FEATURE: service-c\nx", "STATUS: FAIL", START))])
        self.fixture.start(projects_dir=self.projects)
        blocked = self.detail("service-c")
        self.assertEqual(blocked["current_step"], "build")
        self.assertEqual([step["state"] for step in blocked["steps"]], ["done", "done", "failed", "pending", "pending"])

    def test_a_blocked_feature_without_agents_fails_in_the_step_its_note_shows(self):
        self.fixture.start()
        blocked = self.detail("service-c")  # note=gate-rejected
        self.assertEqual(blocked["current_step"], "gate")
        self.assertEqual(blocked["steps"][1]["state"], "failed")

    def test_a_building_feature_whose_last_executor_failed_shows_the_build_step_failed(self):
        self.write_tasks()
        self.setup_session([self.executor("ex1", "T1", START, "STATUS: FAIL")])
        self.fixture.start(projects_dir=self.projects)
        self.assertEqual(self.detail()["steps"][2]["state"], "failed")

    def test_live_the_running_session_executors_are_matched_as_running(self):
        self.write_tasks()
        live_folder = os.path.join(self.fixture.tmp, "claude-projects")
        self.setup_projects(session=SESSION_ID, number=3)  # builds the projects folder and slug dir
        self.fixture.write_json("turns/003.json", {"session_id": SESSION_ID})
        subagents = os.path.join(self.slug_dir, LIVE_SESSION_ID, "subagents")
        prompt = "FEATURE: service-a\nSCOPE_TASK: service-a/T3\nImplement."
        self.write_lines(os.path.join(subagents, "agent-live1.jsonl"), build_unfinished_agent_rows(prompt, START))
        self.fixture.write(os.path.join(subagents, "agent-live1.meta.json"), '{"agentType": "executor-front"}')
        self.fixture.set_lock_alive()
        past = time.time() - 600
        os.utime(self.fixture.at("LOOP.lock"), (past, past))
        os.utime(self.fixture.turns_dir + "/003.json", (past, past))
        for name in os.listdir(os.path.join(self.slug_dir, SESSION_ID)):  # the finished session is old
            for root, _, files in os.walk(os.path.join(self.slug_dir, SESSION_ID, name)):
                for file_name in files:
                    os.utime(os.path.join(root, file_name), (past, past))
        os.utime(os.path.join(self.slug_dir, SESSION_ID + ".jsonl"), (past, past))
        self.fixture.start(projects_dir=live_folder)
        data = self.detail()
        executors = {task["id"]: task["executors"] for task in data["tasks"]["tasks"]}["T3"]
        self.assertEqual(len(executors), 1)
        live = executors[0]
        self.assertEqual((live["state"], live["role"], live["turn"], live["agent_id"]), ("running", "executor-front", 4, "v004:live1"))
        self.assertEqual((live["duration_s"], live["tokens"], live["finished_at"]), (None, None, None))

    def test_live_without_a_session_still_answers_with_the_status_file(self):
        self.write_tasks()
        task_waves.write_status(self.fixture.work, "service-a", "T1", "running")
        self.fixture.set_lock_alive()
        self.fixture.start()
        data = self.detail()
        self.assertEqual({task["id"]: task["status"] for task in data["tasks"]["tasks"]}["T1"], "running")
        self.assertEqual(data["agents"], [])

    def test_live_ignores_a_session_older_than_the_turn(self):
        self.write_tasks()
        subagents = os.path.join(self.fixture.tmp, "claude-projects")
        self.setup_projects(session=SESSION_ID, number=3)
        self.write_lines(os.path.join(self.slug_dir, SESSION_ID, "subagents", "agent-old1.jsonl"),
                         build_unfinished_agent_rows("FEATURE: service-a\nSCOPE_TASK: service-a/T1\nx", START))
        self.fixture.set_lock_alive()
        future = time.time() + 600  # the turn started after everything in the projects folder was written
        os.utime(self.fixture.at("LOOP.lock"), (future, future))
        self.fixture.start(projects_dir=subagents)
        data = self.detail()
        self.assertEqual([call for call in data["agents"] if call["state"] == "running"], [])

    def test_the_agent_log_of_a_live_executor_resolves_through_the_live_session(self):
        self.write_tasks()
        self.setup_projects(session=SESSION_ID, number=3)
        os.remove(self.fixture.turns_dir + "/003.json")  # the running turn has no record
        subagents = os.path.join(self.slug_dir, LIVE_SESSION_ID, "subagents")
        self.write_lines(os.path.join(subagents, "agent-live1.jsonl"),
                         build_unfinished_agent_rows("FEATURE: service-a\nSCOPE_TASK: service-a/T3\nImplement.", START))
        self.fixture.set_lock_alive()
        past = time.time() - 600
        os.utime(self.fixture.at("LOOP.lock"), (past, past))
        self.fixture.start(projects_dir=os.path.join(self.fixture.tmp, "claude-projects"))
        status, data = self.fixture.call("/api/agent/v002:live1/log")
        self.assertEqual(status, 200)
        self.assertTrue(any("Read" in item["text"] for item in data["log"]))


if __name__ == "__main__":
    unittest.main()
