"""T7 and T10: FollowUpThreadRepository (round files, atomic write, current_round) and TaskStatusRepository (tasks.md joined with the status JSON)."""
import json
import os
import shutil
import tempfile
import unittest

from harness_backend.repositories.follow_up_thread_repository import MAX_MESSAGES, FollowUpThreadRepository
from harness_backend.repositories.task_status_repository import TaskStatusRepository
from harness_backend.repositories.specs_file_repository import loop_state

import task_waves

TASKS = ("### T1: First [P]\n**Type**: impl\n**Files**: a.py\n**Deps**: none\n**Done when**: ok\n\n"
         "### T2: Second [P]\n**Type**: impl\n**Files**: b.py\n**Deps**: none\n**Done when**: ok\n\n"
         "### T3: Wire\n**Type**: wiring\n**Files**: c.py\n**Deps**: T1, T2\n**Done when**: ok\n")


class FollowUpThreadRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="thread-repo-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.repository = FollowUpThreadRepository()

    def test_a_missing_file_reads_as_an_empty_thread_of_the_round(self):
        thread = self.repository.read_thread(self.work, 2)
        self.assertEqual((thread["round"], thread["messages"], thread["session_id"], thread["spent_usd"], thread["closed_at"]),
                         (2, [], None, 0.0, None))

    def test_write_then_read_round_trips_and_leaves_no_temp_file(self):
        thread = self.repository.read_thread(self.work, 1)
        thread.update(session_id="abc", spent_usd=1.5, messages=[{"role": "user", "text": "hi"}])
        self.repository.write_thread(self.work, 1, thread)
        again = self.repository.read_thread(self.work, 1)
        self.assertEqual((again["session_id"], again["spent_usd"], again["messages"][0]["text"]), ("abc", 1.5, "hi"))
        folder = self.repository.get_folder_path(self.work)
        self.assertEqual(os.listdir(folder), ["round-1.json"])
        self.assertTrue(os.path.normcase(folder).startswith(os.path.normcase(loop_state.internal_dir(self.work))))

    def test_a_file_with_the_wrong_shape_reads_as_empty(self):
        path = self.repository.get_thread_path(self.work, 1)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for text in ("{broken", json.dumps({"messages": "nope"}), json.dumps([1, 2])):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.assertEqual(self.repository.read_thread(self.work, 1)["messages"], [], text)

    def test_messages_that_are_not_objects_are_dropped_and_the_list_is_capped(self):
        path = self.repository.get_thread_path(self.work, 1)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"messages": [{"role": "user"}, "junk", 3]}, handle)
        self.assertEqual(len(self.repository.read_thread(self.work, 1)["messages"]), 1)
        thread = self.repository.read_thread(self.work, 1)
        thread["messages"] = [{"n": index} for index in range(MAX_MESSAGES + 5)]
        self.repository.write_thread(self.work, 1, thread)
        kept = self.repository.read_thread(self.work, 1)["messages"]
        self.assertEqual((len(kept), kept[-1]["n"]), (MAX_MESSAGES, MAX_MESSAGES + 4))

    def test_current_round_is_one_then_two_after_stage_one_was_closed(self):
        self.assertEqual(self.repository.current_round(self.work, ""), 1)
        thread = self.repository.read_thread(self.work, 1)
        self.repository.write_thread(self.work, 1, thread)
        self.assertEqual(self.repository.current_round(self.work, ""), 1, "an open round stays the current one")
        thread["closed_at"] = "2026-10-21T10:00:00"
        self.repository.write_thread(self.work, 1, thread)
        self.assertEqual(self.repository.current_round(self.work, ""), 2)
        second = self.repository.read_thread(self.work, 2)
        second["closed_at"] = "2026-10-21T11:00:00"
        self.repository.write_thread(self.work, 2, second)
        self.assertEqual(self.repository.current_round(self.work, ""), 3)


class TaskStatusRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="task-repo-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.repository = TaskStatusRepository()
        roadmap = os.path.join(self.work, ".specs", "project", "ROADMAP.md")
        os.makedirs(os.path.dirname(roadmap))
        with open(roadmap, "w", encoding="utf-8") as handle:
            handle.write("# Roadmap\n\n**Max parallel tasks:** 2\n")

    def write_tasks(self, text):
        folder = loop_state.feature_dir(self.work, "feat")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "tasks.md"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_no_tasks_md_returns_an_empty_list(self):
        self.assertEqual(self.repository.read_feature_tasks(self.work, "feat"), {"source": "none", "max_parallel": 2, "tasks": [], "waves": []})

    def test_a_missing_status_file_returns_every_task_pending_in_wave_order(self):
        self.write_tasks(TASKS)
        result = self.repository.read_feature_tasks(self.work, "feat")
        self.assertEqual([(task["id"], task["status"], task["wave"]) for task in result["tasks"]],
                         [("T1", "pending", 1), ("T2", "pending", 1), ("T3", "pending", 2)])
        self.assertEqual(result["tasks"][0]["title"], "First")

    def test_the_status_json_is_joined_by_task_id(self):
        self.write_tasks(TASKS)
        task_waves.write_status(self.work, "feat", "T1", "done", "ok note")
        task_waves.write_status(self.work, "feat", "T2", "running")
        by_id = {task["id"]: task for task in self.repository.read_feature_tasks(self.work, "feat")["tasks"]}
        self.assertEqual((by_id["T1"]["status"], by_id["T1"]["note"]), ("done", "ok note"))
        self.assertEqual(by_id["T2"]["status"], "running")
        self.assertEqual(by_id["T3"]["status"], "pending")

    def test_an_unreadable_status_file_returns_all_pending(self):
        self.write_tasks(TASKS)
        path = task_waves.status_path(self.work, "feat")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("{{{")
        self.assertEqual({task["status"] for task in self.repository.read_feature_tasks(self.work, "feat")["tasks"]}, {"pending"})

    def test_an_unknown_status_value_falls_back_to_pending(self):
        self.write_tasks(TASKS)
        path = task_waves.status_path(self.work, "feat")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump({"tasks": {"T1": {"status": "weird"}}}, handle)
        self.assertEqual(self.repository.read_feature_tasks(self.work, "feat")["tasks"][0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
