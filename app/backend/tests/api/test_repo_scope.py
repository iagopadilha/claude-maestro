"""Scope of the repositories, defined BEFORE the loop: confirm, AI proposal (validated against the catalog) and the Start gate."""
import json
import os
import unittest

from harness_backend.domain.project_errors import InvalidRequestError
from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
from harness_backend.services.loop_control_service import REPOS_NOT_DEFINED_CODE, LoopControlService
from harness_backend.services.repo_scope_proposal_service import RepoScopeProposalService

from .support_multi_repo import MultiRepoTestCase


class StubCatalogService:
    def __init__(self, repos):
        self.repos = repos

    def get_index(self):
        return {"ok": True, "repos": self.repos}


class StubClaudeClient:
    def __init__(self, reply, ok=True):
        self.reply, self.ok, self.prompts = reply, ok, []

    def build_command(self, *arguments, **options):
        return ["claude"]

    def run_prompt(self, argv, folder, prompt, timeout_seconds):
        self.prompts.append(prompt)
        return {"ok": self.ok, "reply": self.reply, "cost_usd": 0.01, "error": "" if self.ok else "boom"}


class TestRepoScope(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("primary")
        self.context = self.make_context(self.primary)
        self.api = self.make_repo("api")
        self.web = self.make_repo("web")
        self.catalog = StubCatalogService([
            {"name": "api", "path": self.api, "purpose": "rest api", "capability_ids": ["orders"]},
            {"name": "web", "path": self.web, "purpose": "front end", "capability_ids": []},
            {"name": "primary", "path": self.primary, "purpose": "this one", "capability_ids": []}])

    def proposal_service(self, reply, ok=True):
        client = StubClaudeClient(reply, ok)
        return RepoScopeProposalService(self.registry, self.catalog, client, WorkspaceFileGuard(), self.log), client

    def test_scope_is_not_defined_until_confirmed_and_confirming_keeps_only_the_primary(self):
        self.assertFalse(self.registry.list_repos(self.context)["defined"])
        listing = self.registry.confirm_scope(self.context)
        self.assertTrue(listing["defined"])
        self.assertEqual([repo["name"] for repo in listing["repos"]], ["primary"])

    def test_adding_a_repo_defines_the_scope(self):
        self.registry.add_repo(self.context, self.api, [])
        self.assertTrue(self.registry.list_repos(self.context)["defined"])

    def test_proposal_keeps_only_catalog_repos_that_are_not_listed(self):
        reply = json.dumps({"repos": [{"name": "api", "reason": "adds the endpoint"}, {"name": "ghost", "reason": "invented"},
                                      {"name": "primary", "reason": "already the primary"}, {"name": "api", "reason": "again"}]})
        service, client = self.proposal_service(reply)
        result = service.propose(self.context, "add an orders endpoint")
        self.assertEqual([(row["name"], row["path"]) for row in result["proposed"]], [("api", self.api)])
        self.assertNotIn('"name": "primary"', client.prompts[0].split("CATALOG (JSON):")[1])  # the primary is not offered
        self.assertFalse(os.path.exists(os.path.join(self.primary, ".specs", "project", "REPOS.json")))  # a proposal writes nothing

    def test_proposal_failure_is_reported_not_raised(self):
        service, _ = self.proposal_service("", ok=False)
        result = service.propose(self.context, "something")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "boom")

    def test_empty_description_is_refused(self):
        service, _ = self.proposal_service("{}")
        with self.assertRaises(InvalidRequestError):
            service.propose(self.context, "  ")

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
