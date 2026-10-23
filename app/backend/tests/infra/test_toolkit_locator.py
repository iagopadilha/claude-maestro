"""`skill_locator.find_toolkit_root` and `harness.py paths`: config.json, plugin cache (newest first, `skills/` directly or under `plugin/`),
sibling clone, and '' when nothing has `skills/`. Temp dirs only: the real ~/.claude, ~/.loop-harness and sibling clone are never looked at."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from harness_backend.config import skill_locator

APP_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HARNESS = os.path.join(APP_ROOT_DIR, "harness.py")


def make_skills(folder):
    os.makedirs(os.path.join(folder, "skills", "backend-dev"), exist_ok=True)
    with open(os.path.join(folder, "skills", "backend-dev", "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write("x")
    return os.path.abspath(folder)


class ToolkitLocatorCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="toolkit-locator-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.claude_home = os.path.join(self.root, "home", ".claude")
        self.cache = os.path.join(self.claude_home, "plugins", "cache")
        self.repo = os.path.join(self.root, "workspace", "claude-maestro")  # REPO_ROOT_DIR stand-in; the sibling is <workspace>/claude-dev-toolkit
        os.makedirs(self.repo)
        environment = mock.patch.dict(os.environ, {"LOOP_CLAUDE_HOME": self.claude_home, "LOOP_DATA_DIR": os.path.join(self.root, "data"),
                                                   "HOME": os.path.join(self.root, "home"), "USERPROFILE": os.path.join(self.root, "home")})
        environment.start()
        self.addCleanup(environment.stop)
        repo_patch = mock.patch.object(skill_locator, "REPO_ROOT_DIR", self.repo)
        repo_patch.start()
        self.addCleanup(repo_patch.stop)

    def cached(self, market, version, nested=False, mtime=None):
        folder = os.path.join(self.cache, market, skill_locator.TOOLKIT_PLUGIN_NAME, version)
        root = make_skills(os.path.join(folder, "plugin") if nested else folder)
        if mtime is not None:
            os.utime(folder, (mtime, mtime))
        return root

    def sibling(self):
        return make_skills(os.path.join(self.root, "workspace", "claude-dev-toolkit", "plugin"))


class TestFindToolkitRoot(ToolkitLocatorCase):
    def test_nothing_installed_gives_an_empty_string(self):
        self.assertEqual(skill_locator.find_toolkit_root({}), "")
        self.assertEqual(skill_locator.find_toolkit_root(), "")

    def test_config_wins_over_the_cache_and_the_sibling(self):
        configured = make_skills(os.path.join(self.root, "configured"))
        self.cached("market", "1.0.0")
        self.sibling()
        self.assertEqual(skill_locator.find_toolkit_root({"dev_toolkit_dir": configured}), configured)

    def test_config_accepts_the_repo_root_and_uses_its_plugin_folder(self):
        repo = os.path.join(self.root, "toolkit-repo")
        plugin = make_skills(os.path.join(repo, "plugin"))
        self.assertEqual(skill_locator.find_toolkit_root({"dev_toolkit_dir": repo}), plugin)

    def test_a_config_folder_without_skills_falls_through(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        cached = self.cached("market", "1.0.0")
        self.assertEqual(skill_locator.find_toolkit_root({"dev_toolkit_dir": empty}), cached)
        self.assertEqual(skill_locator.find_toolkit_root({"dev_toolkit_dir": 42}), cached)
        self.assertEqual(skill_locator.find_toolkit_root({"dev_toolkit_dir": "  "}), cached)

    def test_the_cache_wins_over_the_sibling_clone(self):
        self.sibling()
        cached = self.cached("market", "1.0.0")
        self.assertEqual(skill_locator.find_toolkit_root({}), cached)

    def test_the_newest_cached_version_wins_in_either_layout(self):
        self.cached("market", "0.1.0", mtime=1000)
        newest = self.cached("market", "0.2.0", nested=True, mtime=3000)
        self.cached("other-market", "0.1.5", mtime=2000)
        self.assertEqual(skill_locator.find_toolkit_root({}), newest)

    def test_a_cache_folder_without_skills_is_ignored(self):
        os.makedirs(os.path.join(self.cache, "market", skill_locator.TOOLKIT_PLUGIN_NAME, "0.9.0"))
        self.assertEqual(skill_locator.find_toolkit_root({}), "")

    def test_the_sibling_clone_is_the_last_resort(self):
        sibling = self.sibling()
        self.assertEqual(skill_locator.find_toolkit_root({}), sibling)
        self.assertEqual(os.path.normcase(sibling), os.path.normcase(os.path.join(self.root, "workspace", "claude-dev-toolkit", "plugin")))

    def test_config_is_read_from_the_data_dir_when_not_given(self):
        configured = make_skills(os.path.join(self.root, "from-file"))
        os.makedirs(os.path.join(self.root, "data"))
        with open(os.path.join(self.root, "data", "config.json"), "w", encoding="utf-8") as handle:
            json.dump({"dev_toolkit_dir": configured}, handle)
        self.assertEqual(skill_locator.find_toolkit_root(), configured)

    def test_a_broken_config_file_is_ignored(self):
        os.makedirs(os.path.join(self.root, "data"))
        with open(os.path.join(self.root, "data", "config.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(skill_locator.find_toolkit_root(), "")


class TestPathsCommand(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="harness-paths-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = os.path.join(self.root, "data")
        self.skill = os.path.join(self.root, "plugin", "skills", "orchestrator")
        os.makedirs(self.skill)
        with open(os.path.join(self.skill, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("fake")

    def run_harness(self, *args, toolkit=None):
        env = {name: value for name, value in os.environ.items() if not name.startswith("LOOP_")}
        env.update(LOOP_DATA_DIR=self.data, LOOP_SKILL_DIR=self.skill, LOOP_CLAUDE_HOME=os.path.join(self.root, "claude-home"),
                   HOME=self.root, USERPROFILE=self.root, PYTHONIOENCODING="utf-8")
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "config.json"), "w", encoding="utf-8") as handle:
            json.dump({"dev_toolkit_dir": toolkit} if toolkit else {}, handle)
        return subprocess.run([sys.executable, HARNESS, "paths"] + list(args), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=120)

    def test_json_names_the_three_paths(self):
        toolkit = make_skills(os.path.join(self.root, "toolkit"))
        result = self.run_harness("--json", toolkit=toolkit)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = json.loads(result.stdout)
        self.assertEqual(sorted(paths), ["maestro_plugin", "orchestrator_skill", "toolkit"])
        self.assertEqual(os.path.normcase(paths["toolkit"]), os.path.normcase(toolkit))
        self.assertEqual(os.path.normcase(paths["orchestrator_skill"]), os.path.normcase(os.path.abspath(self.skill)))
        self.assertEqual(os.path.normcase(paths["maestro_plugin"]), os.path.normcase(os.path.abspath(os.path.join(self.skill, "..", ".."))))

    def test_text_is_one_key_value_per_line(self):
        toolkit = make_skills(os.path.join(self.root, "toolkit"))
        lines = self.run_harness(toolkit=toolkit).stdout.splitlines()
        self.assertEqual([line.split("=", 1)[0] for line in lines], ["maestro_plugin", "orchestrator_skill", "toolkit"])
        self.assertTrue(lines[2].split("=", 1)[1])

    def test_a_missing_toolkit_is_an_empty_value_and_exit_zero_unless_the_sibling_clone_exists(self):
        result = self.run_harness("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = json.loads(result.stdout)
        sibling = os.path.join(os.path.dirname(os.path.dirname(APP_ROOT_DIR)), "claude-dev-toolkit", "plugin", "skills")
        if not os.path.isdir(sibling):
            self.assertEqual(paths["toolkit"], "")


if __name__ == "__main__":
    unittest.main()
