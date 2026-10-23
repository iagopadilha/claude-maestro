"""ENV-02 / ENV-03 parity: `engine/credential_check.py` (the app) and the dev toolkit's `shared/testenv.py` (the skills) resolve the SAME winning
layer for names defined in several of the 5 layers. Only temporary folders: the testenv.py under test is a COPY (taken from the toolkit found
by `skill_locator.find_toolkit_root()`; the test is skipped when there is none) placed in a fake toolkit `shared/` folder (its HERE is the
plugin layer, and `dev_toolkit_dir` of the fake config.json points the app at it), LOOP_DATA_DIR, LOOP_USER_SHARED_DIR and the project folder
are temporary, so no real ~/.claude or ~/.loop-harness file is read or written."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402
import credential_check  # noqa: E402
from harness_backend.config import skill_locator  # noqa: E402

# explicit opt-in to the REAL toolkit (the sibling clone / plugin cache): tests/__init__.py hides it from every other lookup
TOOLKIT_ROOT = getattr(skill_locator.find_toolkit_root, "real", skill_locator.find_toolkit_root)({})
TOOLKIT_SHARED_SOURCE = os.path.join(TOOLKIT_ROOT, "shared") if TOOLKIT_ROOT else ""
MAESTRO_SHARED_SOURCE = os.path.join(engine_env.REPO_ROOT_DIR, "plugin", "shared")  # envload.py stays in Maestro's plugin
OVERRIDDEN = ("LOOP_DATA_DIR", "LOOP_SKILL_DIR", "LOOP_USER_SHARED_DIR", "LOOP_CLAUDE_CONFIG_FILE", "LOOP_ENV_FILE",
              "PARITY_ONLY_USER", "PARITY_PLUGIN_USER", "PARITY_MACHINE_ABOVE", "PARITY_PROJECT_ALL", "PARITY_ENV_ALL", "PARITY_NOWHERE")
# Both sides are compared through the SAME layer keys (the labels differ between the app and the script and may be reworded by the toolkit):
# project, machine, toolkit (the toolkit's shared/.env), user-shared, env, missing.
def engine_key_of_label(label):
    return "toolkit" if label == credential_check.PLUGIN_SHARED_LABEL else "user-shared" if label.startswith(credential_check.USER_SHARED_LABEL) else label


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def shared_source(name):
    """`name` from the toolkit's shared/ folder, else (envload.py) from Maestro's plugin/shared/; '' when neither has it."""
    for folder in (TOOLKIT_SHARED_SOURCE, MAESTRO_SHARED_SOURCE):
        if folder and os.path.isfile(os.path.join(folder, name)):
            return os.path.join(folder, name)
    return ""


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


@unittest.skipUnless(TOOLKIT_SHARED_SOURCE and shared_source("testenv.py") and shared_source("envload.py"),
                     "claude-dev-toolkit not found (or without shared/testenv.py): install it or clone it next to this repo")
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
        self.toolkit = os.path.join(self.tmp, "toolkit")
        self.plugin_shared = os.path.join(self.toolkit, "shared")
        os.makedirs(os.path.join(self.toolkit, "skills"))
        os.makedirs(self.plugin_shared, exist_ok=True)
        for name in ("testenv.py", "envload.py"):
            shutil.copyfile(shared_source(name), os.path.join(self.plugin_shared, name))
        self.write_config()
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

    def write_config(self, **extra):
        write(os.path.join(self.data_dir, "config.json"), json.dumps(dict({"dev_toolkit_dir": self.toolkit}, **extra)))

    def restore_environment(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def engine_label(self, name):
        source, label = credential_check.Resolution(self.root).lookup(name)
        return source if source in ("missing", "project", "machine", "env") else engine_key_of_label(label)

    def script_layer_key(self, label):
        """The layer key of a label printed by testenv.py, found through the PATH of that layer (not through its wording)."""
        if label == "environment":
            return "env"
        path = os.path.normcase(os.path.abspath(dict(self.testenv.layer_files(self.root))[label]))
        known = {os.path.join(self.root, ".specs", "project", ".env.test"): "project", os.path.join(self.data_dir, ".env"): "machine",
                 os.path.join(self.plugin_shared, ".env"): "toolkit"}
        return {os.path.normcase(os.path.abspath(key)): value for key, value in known.items()}.get(path, "user-shared")

    def script_label(self, name):
        values, sources = self.testenv.load_layers(self.root)
        return self.script_layer_key(sources[name]) if values.get(name) and name in sources else "missing"

    def test_the_copy_reads_the_fake_plugin_layer_not_the_real_one(self):
        self.assertEqual(os.path.normcase(self.testenv.HERE), os.path.normcase(self.plugin_shared))
        paths = [os.path.normcase(os.path.abspath(path)) for _label, path in self.testenv.layer_files(self.root)]
        plugin_env = os.path.normcase(os.path.join(self.plugin_shared, ".env"))
        self.assertIn(plugin_env, paths)
        self.assertEqual(os.path.normcase(credential_check.toolkit_shared_env_path()), plugin_env)

    def test_the_same_winning_layer_for_names_defined_in_several_layers(self):
        os.environ["PARITY_ENV_ALL"] = "from-the-real-environment"
        expected = {"PARITY_ONLY_USER": "user-shared", "PARITY_PLUGIN_USER": "toolkit", "PARITY_MACHINE_ABOVE": "machine",
                    "PARITY_PROJECT_ALL": "project", "PARITY_ENV_ALL": "env", "PARITY_NOWHERE": "missing"}
        for name, layer in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.script_label(name), layer, "testenv.py")
                self.assertEqual(self.engine_label(name), layer, "credential_check")
                self.assertEqual(self.script_label(name), self.engine_label(name))

    def test_a_blank_value_never_hides_a_lower_layer_in_both(self):
        write(os.path.join(self.root, ".specs", "project", ".env.test"), "PARITY_MACHINE_ABOVE=\n")
        self.assertEqual(self.script_label("PARITY_MACHINE_ABOVE"), "machine")
        self.assertEqual(self.engine_label("PARITY_MACHINE_ABOVE"), "machine")

    def test_the_user_shared_dir_override_through_config_json_agrees(self):
        other = os.path.join(self.tmp, "configured-shared")
        write(os.path.join(other, ".env"), "PARITY_NOWHERE=c\n")
        del os.environ["LOOP_USER_SHARED_DIR"]
        self.write_config(user_shared_dir=other)
        self.assertEqual(self.script_label("PARITY_NOWHERE"), "user-shared")
        self.assertEqual(self.engine_label("PARITY_NOWHERE"), "user-shared")

    def test_without_any_user_shared_folder_neither_side_reads_one(self):
        del os.environ["LOOP_USER_SHARED_DIR"]
        self.assertEqual(self.script_label("PARITY_ONLY_USER"), "missing")
        self.assertEqual(self.engine_label("PARITY_ONLY_USER"), "missing")

    def test_a_name_only_in_the_user_shared_layer_passes_check_in_the_script(self):
        missing = self.testenv.missing_names(self.root, ["PARITY_ONLY_USER", "PARITY_NOWHERE"])
        self.assertEqual(list(missing), ["PARITY_NOWHERE"])


if __name__ == "__main__":
    unittest.main()
