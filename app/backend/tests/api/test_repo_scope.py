"""Scope of the repositories, defined BEFORE the loop: confirm and the Start gate (the chat applies the repos: see test_planning_repos_service)."""
import unittest

from harness_backend.services.loop_control_service import REPOS_NOT_DEFINED_CODE, LoopControlService

from .support_multi_repo import MultiRepoTestCase


class TestRepoScope(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("primary")
        self.context = self.make_context(self.primary)
        self.api = self.make_repo("api")
        self.web = self.make_repo("web")

    def test_scope_is_defined_from_the_start_without_a_confirm_step(self):
        self.assertTrue(self.registry.list_repos(self.context)["defined"])  # RP-01: the primary is edit, nothing to confirm
        listing = self.registry.confirm_scope(self.context)
        self.assertTrue(listing["defined"])
        self.assertEqual([repo["name"] for repo in listing["repos"]], ["primary"])

    def test_adding_a_repo_defines_the_scope(self):
        self.registry.add_repo(self.context, self.api, [])
        self.assertTrue(self.registry.list_repos(self.context)["defined"])

    def test_start_asks_for_the_definition_before_anything_else(self):
        service = LoopControlService.__new__(LoopControlService)
        service.__init__(None, self.log, self.loop_status_service, None, None, None, lambda: "")
        service.scope_defined_provider = lambda context: False
        service.resolve_flags_and_dispatch = lambda context, body: ([], None, None)
        service.is_loop_alive = lambda context: False
        status, body = service.start_new_loop(self.context, {})
        self.assertEqual((status, body["code"]), (409, REPOS_NOT_DEFINED_CODE))


if __name__ == "__main__":
    unittest.main()
