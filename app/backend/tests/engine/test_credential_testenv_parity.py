"""ENV-02 / ENV-03 parity: `engine/credential_check.py` (the app) and `plugin/shared/testenv.py` (the skills) resolve the SAME winning layer for
names defined in several of the 5 layers. Only temporary folders: the testenv.py under test is a COPY placed in a fake plugin `shared/` folder
(its HERE is the plugin layer), LOOP_DATA_DIR, LOOP_USER_SHARED_DIR and the project folder are temporary, so no real ~/.claude or
~/.loop-harness file is read or written."""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402
import credential_check  # noqa: E402

PLUGIN_SHARED_SOURCE = os.path.join(engine_env.REPO_ROOT_DIR, "plugin", "shared")
OVERRIDDEN = ("LOOP_DATA_DIR", "LOOP_SKILL_DIR", "LOOP_USER_SHARED_DIR", "LOOP_CLAUDE_CONFIG_FILE", "LOOP_ENV_FILE",
              "PARITY_ONLY_USER", "PARITY_PLUGIN_USER", "PARITY_MACHINE_ABOVE", "PARITY_PROJECT_ALL", "PARITY_ENV_ALL", "PARITY_NOWHERE")
# credential_check source/label -> the label testenv.py prints for the same layer
TESTENV_LABEL_BY_SOURCE = {"project": "project .env.test", "machine": "user ~/.loop-harness/.env",
                           credential_check.PLUGIN_SHARED_LABEL: "plugin shared/.env",
                           credential_check.USER_SHARED_LABEL: "user shared ~/.claude/skills/_shared/.env",
                           "env": "environment"}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def load_copy_of_testenv(plugin_shared_dir):
    """Imports the temp copy of testenv.py by path (its `import envload` finds the copied envload.py next to it)."""
    saved = sys.modules.pop("envload", None)
    try:
        spec = importlib.util.spec_from_file_location("testenv_parity_copy", os.path.join(plugin_shared_dir, "testenv.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("envload", None)
        if saved is not None:
            sys.modules["envload"] = saved
    return module


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in OVERRIDDEN}
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="parity-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "project")
        self.data_dir = os.path.join(self.tmp, "data")
        self.user_shared = os.path.join(self.tmp, "user-shared")
        skill = os.path.join(self.tmp, "plugin", "skills", "orchestrator")
        engine_env.create_fake_skill(skill, copy_guard=False)
        self.plugin_shared = os.path.join(self.tmp, "plugin", "shared")
        os.makedirs(self.plugin_shared, exist_ok=True)
        for name in ("testenv.py", "envload.py"):
            shutil.copyfile(os.path.join(PLUGIN_SHARED_SOURCE, name), os.path.join(self.plugin_shared, name))
        os.makedirs(os.path.join(self.root, ".specs", "project"))
        for name in OVERRIDDEN:
            os.environ.pop(name, None)
        os.environ.update({"LOOP_DATA_DIR": self.data_dir, "LOOP_SKILL_DIR": skill, "LOOP_USER_SHARED_DIR": self.user_shared,
                           "LOOP_CLAUDE_CONFIG_FILE": os.path.join(self.tmp, "claude.json")})
        self.addCleanup(self.restore_environment)
        write(os.path.join(self.user_shared, ".env"),
              "PARITY_ONLY_USER=u\nPARITY_PLUGIN_USER=u\nPARITY_MACHINE_ABOVE=u\nPARITY_PROJECT_ALL=u\nPARITY_ENV_ALL=u\n")
        write(os.path.join(self.plugin_shared, ".env"), "PARITY_PLUGIN_USER=p\nPARITY_MACHINE_ABOVE=p\nPARITY_PROJECT_ALL=p\nPARITY_ENV_ALL=p\n")
        write(os.path.join(self.data_dir, ".env"), "PARITY_MACHINE_ABOVE=m\nPARITY_PROJECT_ALL=m\nPARITY_ENV_ALL=m\n")
        write(os.path.join(self.root, ".specs", "project", ".env.test"), "PARITY_PROJECT_ALL=j\nPARITY_ENV_ALL=j\n")
        self.testenv = load_copy_of_testenv(self.plugin_shared)

    def restore_environment(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def engine_label(self, name):
        source, label = credential_check.Resolution(self.root).lookup(name)
        return "missing" if source == "missing" else TESTENV_LABEL_BY_SOURCE.get(source if source in ("project", "machine", "env") else label)

    def script_label(self, name):
        values, sources = self.testenv.load_layers(self.root)
        return sources.get(name, "missing") if values.get(name) else "missing"

    def test_the_copy_reads_the_fake_plugin_layer_not_the_real_one(self):
        self.assertEqual(os.path.normcase(self.testenv.HERE), os.path.normcase(self.plugin_shared))
        self.assertIn("plugin shared/.env", [label for label, _path in self.testenv.layer_files(self.root)])

    def test_the_same_winning_layer_for_names_defined_in_several_layers(self):
        os.environ["PARITY_ENV_ALL"] = "from-the-real-environment"
        expected = {"PARITY_ONLY_USER": "user shared ~/.claude/skills/_shared/.env",
                    "PARITY_PLUGIN_USER": "plugin shared/.env",
                    "PARITY_MACHINE_ABOVE": "user ~/.loop-harness/.env",
                    "PARITY_PROJECT_ALL": "project .env.test",
                    "PARITY_ENV_ALL": "environment",
                    "PARITY_NOWHERE": "missing"}
        for name, layer in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.script_label(name), layer, "testenv.py")
                self.assertEqual(self.engine_label(name), layer, "credential_check")
                self.assertEqual(self.script_label(name), self.engine_label(name))

    def test_a_blank_value_never_hides_a_lower_layer_in_both(self):
        write(os.path.join(self.root, ".specs", "project", ".env.test"), "PARITY_MACHINE_ABOVE=\n")
        self.assertEqual(self.script_label("PARITY_MACHINE_ABOVE"), "user ~/.loop-harness/.env")
        self.assertEqual(self.engine_label("PARITY_MACHINE_ABOVE"), "user ~/.loop-harness/.env")

    def test_the_user_shared_dir_override_through_config_json_agrees(self):
        other = os.path.join(self.tmp, "configured-shared")
        write(os.path.join(other, ".env"), "PARITY_NOWHERE=c\n")
        del os.environ["LOOP_USER_SHARED_DIR"]
        write(os.path.join(self.data_dir, "config.json"), '{"user_shared_dir": %s}' % __import__("json").dumps(other))
        self.assertEqual(self.script_label("PARITY_NOWHERE"), "user shared ~/.claude/skills/_shared/.env")
        self.assertEqual(self.engine_label("PARITY_NOWHERE"), "user shared ~/.claude/skills/_shared/.env")

    def test_a_name_only_in_the_user_shared_layer_passes_check_in_the_script(self):
        missing = self.testenv.missing_names(self.root, ["PARITY_ONLY_USER", "PARITY_NOWHERE"])
        self.assertEqual(list(missing), ["PARITY_NOWHERE"])


if __name__ == "__main__":
    unittest.main()
