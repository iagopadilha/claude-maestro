"""Planning mode on the loop COMMANDS module: a project that never approved a roadmap and never looped refuses the loop
commands, the chat, the Act mode and the whiteboard writes with 409 `code:"planning_required"`; reads stay open."""
import os
import unittest

from tests.api.support_commands import CommandsTestCase, loop_state

GUARDED_POSTS = (("answer", {"text": "x"}), ("resume", {}), ("note", {"text": "x"}), ("pause", {}), ("loop/check", {}),
                 ("loop/start", {}), ("instruction", {"text": "x"}), ("whiteboard", {"data": {}, "base_version": 0}),
                 ("chat/send", {"text": "x"}), ("action/propose", {"text": "deliver"}), ("action/run", {"action": "summary", "confirm": True}))


class PlanningRequiredTests(CommandsTestCase):
    def start_fresh_project(self):
        """Project without a loop worktree, LOOP.json, meta turns or turn files: only a ROADMAP (never approved)."""
        fresh = os.path.join(self.fixture.tmp, "fresh")
        self.fixture.write(os.path.join(fresh, ".specs", "project", "ROADMAP.md"), "## Stage 1\n- [a] status=pending\n")
        self.fixture.start(repo=fresh)
        return fresh

    def test_every_command_is_refused_in_planning_mode(self):
        self.start_fresh_project()
        for path, body in GUARDED_POSTS:
            status, data = self.fixture.call("/" + path, "POST", body)
            self.assertEqual((status, data.get("code"), data.get("ok")), (409, "planning_required", False), path)
        self.assertEqual(self.fixture.call("/whiteboard")[0], 200, "reads stay open")
        self.assertEqual(self.fixture.call("/chat/threads")[0], 200)
        self.assertEqual(self.fixture.call("/shutdown", "POST", {})[0], 200)

    def test_the_approval_or_a_loop_trace_ends_planning_mode(self):
        fresh = self.start_fresh_project()
        loop_state.approve_roadmap(fresh, "2026-10-16T10:00:00")
        status, data = self.fixture.call("/note", "POST", {"text": "x"})
        self.assertEqual(status, 409)
        self.assertNotIn("code", data, "refused by the loop rule (not running), no longer by planning mode")
        os.remove(loop_state.loop_path(fresh, loop_state.APPROVAL_NAME))
        self.assertEqual(self.fixture.call("/note", "POST", {"text": "x"})[1].get("code"), "planning_required")
        self.fixture.write(loop_state.state_file(fresh, "LOOP.json"), '{"status": "done"}')
        self.assertNotIn("code", self.fixture.call("/note", "POST", {"text": "x"})[1], "a project that already looped keeps the banner")

    def test_project_with_loop_worktree_is_never_in_planning_mode(self):
        self.fixture.start()  # default fixture: repo + loop worktree, roadmap not approved
        status, data = self.fixture.call("/whiteboard", "POST", {"data": {}, "base_version": 0})
        self.assertEqual(status, 200, data)


if __name__ == "__main__":
    unittest.main()
