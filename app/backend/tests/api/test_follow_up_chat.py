"""T11 (FUC-02..12): the follow-up as a chat. One claude session per round (`--resume` on the 2nd message, fresh-session fallback), a turn that
breaks the rules is reverted to the text BEFORE THE TURN (the earlier good turn stays), the US$ 5 round budget (409 `follow_up_budget`, run still
accepted), round n+1 after Run, and `finished` once the worktree is gone. Reuses the fakes of test_follow_up_service."""
import os
import shutil
import unittest

from harness_backend.domain.project_errors import RepoConflictError
from harness_backend.repositories.follow_up_thread_repository import FollowUpThreadRepository

from . import test_follow_up_service as base

SECOND_STAGE = ('\n## Stage Follow-up-2 - Follow-up 2\n\n'
                '- [round-two] status=pending scope=Small kind=back paths=src/two deps=base title="Round two"\n')


class SessionClaudeClient(base.FakeClaudeClient):
    """Returns a session id and a configurable cost; `script` (a list of (ok, error) per call) lets a test fail a resumed turn."""

    def __init__(self):
        super().__init__()
        self.cost = 0.1
        self.session = "sess-1"
        self.outcomes = []

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None):
        self.calls.append({"argv": argv, "folder": folder, "stdin": stdin_text})
        ok, error = self.outcomes.pop(0) if self.outcomes else (True, "")
        reply = self.behavior(folder) if ok else ""
        return {"ok": ok, "reply": reply, "cost_usd": self.cost, "session_id": self.session if ok else "", "error": error}


class ChatTestCase(base.FollowUpTestCase):
    def setUp(self):
        super().setUp()
        self.client = SessionClaudeClient()
        self.jobs.claude_client = self.client
        self.service.claude_client = self.client
        self.threads = FollowUpThreadRepository()

    def edit_stage(self, old, new):
        path = self.path("ROADMAP.md")
        self.write(path, self.read(path).replace(old, new))

    def thread(self, round_number=1):
        return self.threads.read_thread(self.work, round_number)

    def argv_of(self, call_index):
        return self.client.calls[call_index]["argv"]


class TestSession(ChatTestCase):
    def test_two_messages_continue_one_session_and_the_second_may_edit_the_stage_of_the_first(self):
        self.client.behavior = self.append_stage
        self.assertEqual(self.send(text="add a report")["state"], "ok")
        self.assertNotIn("--resume", self.argv_of(0))
        self.client.behavior = lambda folder: self.edit_stage('title="Extra report"', 'title="Extra report v2"')
        second = self.send(text="rename it")
        self.assertEqual(second["state"], "ok", second)
        argv = self.argv_of(1)
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")
        self.assertIn("Extra report v2", self.read(self.path("ROADMAP.md")))
        thread = self.thread()
        self.assertEqual([(message["role"], message.get("state")) for message in thread["messages"]],
                         [("user", None), ("assistant", "ok"), ("user", None), ("assistant", "ok")])
        self.assertEqual(thread["messages"][3]["changed"], {"added": [], "removed": [], "edited": ["extra-report"]})
        self.assertEqual(thread["messages"][1]["changed"]["added"], ["extra-report"])
        self.assertAlmostEqual(thread["spent_usd"], 0.2)
        self.assertEqual(thread["baseline"]["roadmap"], base.ROADMAP, "the round baseline is taken once, before the first turn")

    def test_a_resume_that_lost_its_session_is_retried_once_as_a_fresh_session(self):
        self.client.behavior = self.append_stage
        self.send(text="first")
        self.client.outcomes = [(False, "No conversation found with session ID sess-1")]
        self.client.behavior = lambda folder: self.edit_stage('title="Extra report"', 'title="Retried"')
        job = self.send(text="second")
        self.assertEqual(job["state"], "ok", job)
        self.assertEqual(len(self.client.calls), 3, "first, the failed resume and the fresh retry")
        self.assertIn("--resume", self.argv_of(1))
        self.assertNotIn("--resume", self.argv_of(2))
        self.assertIn("Earlier messages were summarized", self.thread()["summary_note"])

    def test_any_other_failure_is_not_retried(self):
        self.client.behavior = self.append_stage
        self.send(text="first")
        self.client.outcomes = [(False, "timeout after 900 s")]
        self.assertEqual(self.send(text="second")["state"], "error")
        self.assertEqual(len(self.client.calls), 2)


class TestRevertToBeforeTheTurn(ChatTestCase):
    def test_a_turn_that_touches_an_executed_stage_is_reverted_and_the_earlier_turn_stays(self):
        self.client.behavior = self.append_stage
        self.assertEqual(self.send(text="add")["state"], "ok")
        after_first = (self.read(self.path("ROADMAP.md")), self.read(self.path("FLOWS.md")))

        def bad_turn(folder):
            self.edit_stage("status=verified", "status=pending")
            self.edit_stage('title="Extra report"', 'title="Changed in the bad turn"')
            return "oops"
        self.client.behavior = bad_turn
        job = self.send(text="break it")
        self.assertEqual(job["state"], "error")
        self.assertIn("above the Follow-up 1 stage", job["error"])
        self.assertEqual((self.read(self.path("ROADMAP.md")), self.read(self.path("FLOWS.md"))), after_first,
                         "back to the text before THIS turn, not to the round baseline")
        self.assertIn("Extra report", after_first[0])
        last = self.thread()["messages"][-1]
        self.assertEqual((last["state"], last["changed"]), ("error", {"added": [], "removed": [], "edited": []}))
        self.assertGreater(self.thread()["spent_usd"], 0.19, "a failed turn still cost money")

    def test_a_slug_of_an_earlier_stage_is_refused_naming_it(self):
        self.client.behavior = lambda folder: self.write(self.path("ROADMAP.md"), base.ROADMAP + base.NEW_STAGE.replace("extra-report", "base"))
        job = self.send()
        self.assertEqual(job["state"], "error")
        self.assertIn("`base`", job["error"])


class TestRoundBudget(ChatTestCase):
    def test_after_five_dollars_send_is_409_follow_up_budget_while_run_is_still_accepted(self):
        self.client.cost = 2.5
        self.client.behavior = self.append_stage
        self.send(text="one")
        self.client.behavior = lambda folder: "no change"
        self.send(text="two")
        self.assertAlmostEqual(self.thread()["spent_usd"], 5.0)
        state = self.service.get_follow_up(self.context)
        self.assertFalse(state["can_send"]["ok"])
        self.assertIn("budget", state["can_send"]["reason"])
        self.assertTrue(state["can_continue"]["ok"])
        with self.assertRaises(RepoConflictError) as caught:
            self.service.send_follow_up(self.context, {"text": "three"})
        self.assertEqual(getattr(caught.exception, "code", None), "follow_up_budget")
        status, _payload = self.service.run_follow_up(self.context)
        self.assertEqual(status, 202)

    def test_caps_are_reported(self):
        self.assertEqual(self.service.get_follow_up(self.context)["caps"], {"message_usd": 1, "round_usd": 5})


class TestNextRound(ChatTestCase):
    def test_after_run_the_next_send_opens_round_two_with_zero_spend(self):
        self.client.behavior = self.append_stage
        self.send(text="one")
        self.assertEqual(self.service.get_follow_up(self.context)["round"], 1)
        self.service.run_follow_up(self.context)
        self.assertIsNotNone(self.thread(1)["closed_at"])
        state = self.service.get_follow_up(self.context)
        self.assertEqual((state["round"], state["thread"]["messages"], state["thread"]["spent_usd"]), (2, [], 0.0))
        self.client.behavior = lambda folder: self.write(self.path("ROADMAP.md"), self.read(self.path("ROADMAP.md")) + SECOND_STAGE)
        job = self.send(text="more")
        self.assertEqual(job["state"], "ok", job)
        self.assertNotIn("--resume", self.argv_of(1), "a new round starts a new session")
        self.assertIn("Follow-up-2", self.client.calls[1]["stdin"])
        self.assertEqual(self.service.get_follow_up(self.context)["stage"]["features"][0]["slug"], "round-two")
        self.assertEqual(len(self.thread(1)["messages"]), 2, "round 1 keeps its own thread")

    def test_round_two_cannot_edit_the_stage_of_round_one(self):
        self.client.behavior = self.append_stage
        self.send(text="one")
        self.service.run_follow_up(self.context)

        def edits_round_one(folder):
            self.edit_stage('title="Extra report"', 'title="Reworded"')
            self.write(self.path("ROADMAP.md"), self.read(self.path("ROADMAP.md")) + SECOND_STAGE)
        self.client.behavior = edits_round_one
        job = self.send(text="more")
        self.assertEqual(job["state"], "error")
        self.assertIn("above the Follow-up 2 stage", job["error"])


class TestFinished(ChatTestCase):
    def test_after_the_worktree_is_removed_get_reports_finished_and_send_is_refused(self):
        self.client.behavior = self.append_stage
        self.send(text="one")
        self.assertFalse(self.service.get_follow_up(self.context)["finished"])
        shutil.rmtree(self.work)
        state = self.service.get_follow_up(self.context)
        self.assertTrue(state["finished"])
        self.assertFalse(state["can_send"]["ok"])
        self.assertFalse(state["can_continue"]["ok"])
        self.assertIn("Back to planning", state["can_send"]["reason"])
        with self.assertRaises(RepoConflictError):
            self.service.send_follow_up(self.context, {"text": "x"})
        self.assertTrue(os.path.isdir(self.repo))


if __name__ == "__main__":
    unittest.main()
