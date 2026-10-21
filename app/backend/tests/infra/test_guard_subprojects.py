"""Guard profiles of the monorepo subprojects feature: `map-docs` also allows `<path>/docs/codebase/` of confirmed subprojects, profile
`subprojects` (rule `subprojects-only`) and profile `systemmap` (rule `system-only`). The hook runs as a process with a JSON payload on stdin."""
import importlib.util
import json
import os
import re
import unittest

from backend.tests.infra.test_guard_sql_write import GuardTestCase, GUARDS, PLUGIN_GUARD, USER_GUARD

SUGGESTED = ".specs/project/SUBPROJECTS-SUGGESTED.json"


class SubprojectsGuardTests(GuardTestCase):
    def write_list(self, confirmed=True, monorepo=True, skip_tools=True, raw=None):
        entry = {"monorepo": monorepo, "confirmed": confirmed, "projects": [
            {"path": "backend", "name": "backend", "skip": False}, {"path": "tools/seed", "name": "seed", "skip": skip_tools}]}
        with open(os.path.join(self.root, ".specs", "project", "SUBPROJECTS.json"), "w", encoding="utf-8") as handle:
            handle.write(raw if raw is not None else json.dumps({"version": 1, "repos": {"main": entry}}))

    def write_target(self, profile, relative, tool="Write"):
        return self.run_guard(profile, tool, {"file_path": os.path.join(self.root, relative)})

    def assertAllowed(self, result):
        self.assertEqual(result[0], 0, result[1])

    def assertBlocked(self, result, rule=None):
        self.assertEqual(result[0], 2, result[1])
        if rule:
            self.assertIn("[%s]" % rule, result[1])

    def test_spec_profile_allows_confirmed_subproject_map(self):
        self.write_list()
        self.assertAllowed(self.write_target("spec", "backend/docs/codebase/STACK.md"))
        self.assertAllowed(self.write_target("spec", "docs/codebase/SYSTEM.md"))
        self.assertBlocked(self.write_target("spec", "backend/src/x.cs"), "specs-only")
        self.assertBlocked(self.write_target("spec", "frontend/docs/codebase/STACK.md"), "specs-only")

    def test_skipped_subproject_has_no_map_folder(self):
        self.write_list(skip_tools=True)
        self.assertBlocked(self.write_target("spec", "tools/seed/docs/codebase/STACK.md"), "specs-only")
        self.write_list(skip_tools=False)
        self.assertAllowed(self.write_target("spec", "tools/seed/docs/codebase/STACK.md"))

    def test_unconfirmed_missing_or_broken_list_allows_only_root_map(self):
        for label, prepare in (("unconfirmed", lambda: self.write_list(confirmed=False)), ("not a monorepo", lambda: self.write_list(monorepo=False)),
                               ("broken json", lambda: self.write_list(raw="{not json")), ("odd shape", lambda: self.write_list(raw='{"repos": [1]}')),
                               ("no file", lambda: None)):
            with self.subTest(label):
                prepare()
                self.assertBlocked(self.write_target("spec", "backend/docs/codebase/STACK.md"), "specs-only")
                self.assertAllowed(self.write_target("spec", "docs/codebase/STACK.md"))

    def test_planning_profile_never_gets_map_docs(self):
        self.write_list()
        self.assertBlocked(self.write_target("planning", "backend/docs/codebase/STACK.md"), "specs-only")

    def test_traversal_in_the_list_adds_nothing(self):
        self.write_list(raw=json.dumps({"repos": {"main": {"monorepo": True, "confirmed": True, "projects": [
            {"path": "../outside"}, {"path": "a/../../b"}, {"path": "/abs"}, {"path": "C:/x"}, {"path": "."}, {"path": "ok/"}]}}}))
        for guard in GUARDS:
            spec = importlib.util.spec_from_file_location("guard_under_test", guard)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.confirmed_subproject_map_folders(os.path.join(self.root, ".specs")), ["ok/docs/codebase/"])

    def test_subprojects_profile_writes_only_the_suggestion(self):
        self.write_list()
        self.assertAllowed(self.write_target("subprojects", SUGGESTED))
        self.assertAllowed(self.write_target("subprojects", SUGGESTED, tool="Edit"))
        for relative in (".specs/project/SUBPROJECTS.json", ".specs/project/ROADMAP.md", "docs/codebase/SYSTEM.md", "backend/docs/codebase/STACK.md",
                         "backend/src/x.cs"):
            with self.subTest(relative):
                self.assertBlocked(self.write_target("subprojects", relative), "subprojects-only")
        self.assertBlocked(self.bash("subprojects", "echo x > .specs/project/SUBPROJECTS.json"))
        self.assertBlocked(self.bash("subprojects", "git commit -m x"))
        self.assertAllowed(self.bash("subprojects", "echo x > %s" % SUGGESTED))

    def test_systemmap_profile_writes_only_system_md(self):
        self.write_list()
        self.assertAllowed(self.write_target("systemmap", "docs/codebase/SYSTEM.md"))
        for relative in (SUGGESTED, ".specs/project/ROADMAP.md", "docs/codebase/STACK.md", "backend/docs/codebase/STACK.md", "backend/src/x.cs"):
            with self.subTest(relative):
                self.assertBlocked(self.write_target("systemmap", relative), "system-only")
        self.assertBlocked(self.bash("systemmap", "echo x > docs/codebase/STACK.md"))
        self.assertAllowed(self.bash("systemmap", "echo x > docs/codebase/SYSTEM.md"))

    def test_new_profiles_keep_the_planning_hardening(self):
        for profile in ("subprojects", "systemmap"):
            with self.subTest(profile):
                self.assertBlocked(self.bash(profile, "git push origin feature/x"))
                self.assertBlocked(self.query(profile, "DELETE FROM t"), "sql-write")

    def test_inventory_cli_is_not_blocked_but_loop_commands_are(self):
        command = 'python "C:/app/harness.py" inventory . --changed backend/a.cs --json'
        for profile in ("spec", "verifier", "executor", "orchestrator"):
            with self.subTest(profile):
                self.assertAllowed(self.bash(profile, command))
        self.assertBlocked(self.bash("verifier", 'python "C:/app/harness.py" loop --project x'), "loop-run")


class GuardCopiesTests(unittest.TestCase):
    NAMES = ("subprojects-only", "system-only", "map-docs", "confirmed_subproject_map_folders", "SUBPROJECTS_SUGGESTED_PATH", "SYSTEM_MAP_PATH")

    def test_both_copies_define_the_same_new_rules(self):
        if not os.path.isfile(USER_GUARD):
            self.skipTest("no ~/.claude guard copy")
        texts = {path: open(path, encoding="utf-8").read() for path in (PLUGIN_GUARD, USER_GUARD)}
        for name in self.NAMES:
            for path, text in texts.items():
                self.assertIn(name, text, "%s misses %s" % (path, name))
        profiles = [re.findall(r'^    "(subprojects|systemmap|spec|planning)": \[(.*)\],$', text, re.M) for text in texts.values()]
        self.assertEqual(profiles[0], profiles[1])
        function = [re.search(r"def confirmed_subproject_map_folders.*?\n\n\n", text, re.S).group(0).replace("\r\n", "\n") for text in texts.values()]
        self.assertEqual(function[0], function[1])


if __name__ == "__main__":
    unittest.main()
