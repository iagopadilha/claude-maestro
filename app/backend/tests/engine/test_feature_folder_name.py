"""The feature folder is the slug with ':' (and any character a Windows path cannot hold) replaced by '-': one helper, used by the contract
merge, the missing-contract warning, the spec text, the SQL script order, the specs tree and the spec readers. A legacy folder named with
the raw slug is still read when it exists (a system that allows ':')."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import audit_deps  # noqa: E402
import loop_state  # noqa: E402
import prod_database_script  # noqa: E402
import run_loop  # noqa: E402
import system_map  # noqa: E402

SLUG = "backend:version-endpoint"
FOLDER = "backend-version-endpoint"
SYSTEM_TEXT = "# System\n\n<!-- CONTRACT:BEGIN -->\n<!-- CONTRACT:END -->\n\n## Run\n"
CONTRACT = "- [add] route GET /version — build version (Api/VersionController.cs)\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class TestFeatureFolderName(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="feature-folder-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_colon_and_other_invalid_characters_become_a_dash(self):
        self.assertEqual(loop_state.feature_folder_name(SLUG), FOLDER)
        self.assertEqual(loop_state.feature_folder_name('a<b>c"d|e?f*g'), "a-b-c-d-e-f-g")
        self.assertEqual(loop_state.feature_folder_name("plain-slug"), "plain-slug")

    def test_feature_dir_uses_the_folder_name_and_the_legacy_raw_folder_only_when_it_is_the_one_that_exists(self):
        expected = os.path.join(self.root, ".specs", "features", FOLDER)
        self.assertEqual(loop_state.feature_dir(self.root, SLUG), expected)
        legacy = os.path.join(self.root, ".specs", "features", SLUG)
        try:
            os.makedirs(legacy)
        except OSError:
            self.skipTest("this system cannot hold ':' in a folder name")
        self.assertEqual(loop_state.feature_dir(self.root, SLUG), legacy)
        os.makedirs(expected)
        self.assertEqual(loop_state.feature_dir(self.root, SLUG), expected)

    def test_contract_path_and_pending_line_name_the_folder(self):
        self.assertTrue(system_map.contract_path(self.root, SLUG).endswith(os.path.join("features", FOLDER, "contract.md")))
        self.assertEqual(system_map.pending_line(SLUG), "CONTRACT-PENDING: .specs/features/%s/contract.md" % FOLDER)

    def test_contract_in_the_folder_of_a_colon_slug_is_merged(self):
        write(os.path.join(self.root, system_map.SYSTEM_RELATIVE_PATH), SYSTEM_TEXT)
        write(os.path.join(self.root, ".specs", "features", FOLDER, "contract.md"), CONTRACT)
        result = system_map.merge_contract_deltas(self.root, [SLUG])
        self.assertTrue(result["changed"])
        self.assertEqual(result["warnings"], [])
        with open(os.path.join(self.root, system_map.SYSTEM_RELATIVE_PATH), encoding="utf-8") as handle:
            self.assertIn("GET /version", handle.read())

    def test_hook_merges_and_the_missing_contract_warning_names_the_real_folder(self):
        specs = os.path.join(self.root, ".specs", "project")
        write(os.path.join(specs, "ROADMAP.md"), "## Stage 1\n- [%s] status=verified kind=back paths=api\n" % SLUG)
        write(os.path.join(specs, "SUBPROJECTS.json"), json.dumps({"version": 1, "repos": {os.path.basename(self.root): {
            "monorepo": True, "confirmed": True, "projects": [{"path": "api", "name": "api", "stack": "dotnet", "skill": "backend-dev", "skip": False}]}}}))
        write(os.path.join(self.root, system_map.SYSTEM_RELATIVE_PATH), SYSTEM_TEXT)
        meta, state = {}, {"status": "continue", "turn": 1}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run_loop.merge_verified_contracts(self.root, meta, state, specs)
        self.assertIn(".specs/features/%s/contract.md" % FOLDER, output.getvalue())
        self.assertNotIn(SLUG + "/contract.md", output.getvalue())
        meta, state = {}, {"status": "continue", "turn": 2}
        write(os.path.join(self.root, ".specs", "features", FOLDER, "contract.md"), CONTRACT)
        with contextlib.redirect_stdout(io.StringIO()):
            run_loop.merge_verified_contracts(self.root, meta, state, specs)
        with open(os.path.join(self.root, system_map.SYSTEM_RELATIVE_PATH), encoding="utf-8") as handle:
            self.assertIn("GET /version", handle.read())

    def test_spec_text_reads_the_folder(self):
        write(os.path.join(self.root, ".specs", "features", FOLDER, "spec.md"), "line one\n")
        self.assertEqual(audit_deps.spec_text(self.root, SLUG), [(".specs/features/%s/spec.md" % FOLDER, 1, "line one")])

    def test_sql_scripts_follow_the_roadmap_order_through_the_folder_names(self):
        write(os.path.join(self.root, ".specs", "project", "ROADMAP.md"),
              "## Stage 1\n- [%s] status=verified paths=api\n- [aaa-first] status=verified paths=api\n" % SLUG)
        write(os.path.join(self.root, ".specs", "features", "aaa-first", "sql", "1.sql"), "select 1;\n")
        write(os.path.join(self.root, ".specs", "features", FOLDER, "sql", "1.sql"), "select 2;\n")
        found = prod_database_script.manual_scripts(self.root, roadmap_root=self.root)
        self.assertEqual([item[0] for item in found], [FOLDER, "aaa-first"])


if __name__ == "__main__":
    unittest.main()
