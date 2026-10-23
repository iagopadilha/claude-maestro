#!/usr/bin/env python3
"""Runtime isolation of the `claude -p` argv (engine/runtime_isolation.py): bundle build, skipped skills, signature cache, atomic swap,
allowlists, inline settings, and the loop turns' argv with the kill switch on and off. Temp dirs only: no real ~/.claude or ~/.loop-harness."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
from test_loop import LoopCase  # noqa: E402
import run_loop  # noqa: E402
import runtime_isolation as iso  # noqa: E402

ROAD = "## Stage 1\n- [a] status=pending\n"
GUARD_PROFILE_KEYS = {"orchestrator", "planning", "roadmapmap", "subprojects", "systemmap", "spec", "executor", "verifier"}


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


class World(unittest.TestCase):
    """A fake plugin (agents + skills), a fake user skills folder and a data dir, all temporary."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="isolation-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.plugin = os.path.join(self.root, "plugin")
        self.user_skills = os.path.join(self.root, "home", ".claude", "skills")
        self.data = os.path.join(self.root, "data")
        for agent in ("executor-back", "verifier"):
            write(os.path.join(self.plugin, "agents", agent + ".md"), "agent " + agent)
        write(os.path.join(self.plugin, "skills", "orchestrator", "SKILL.md"), "plugin orchestrator")
        write(os.path.join(self.plugin, "skills", "repo-catalog", "SKILL.md"), "plugin repo-catalog")
        write(os.path.join(self.user_skills, "repo-catalog", "SKILL.md"), "user repo-catalog")  # the user's version wins
        write(os.path.join(self.user_skills, "backend-dev", "SKILL.md"), "user backend-dev")
        write(os.path.join(self.user_skills, "backend-dev", "references", "a.md"), "ref")
        iso.clear_memo()
        self.addCleanup(iso.clear_memo)

    def set_env(self, **values):
        for name, value in values.items():
            previous = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(lambda n=name, p=previous: os.environ.pop(n, None) if p is None else os.environ.__setitem__(n, p))

    def ensure(self, names=("orchestrator", "repo-catalog", "backend-dev", "ghost"), **kwargs):
        return iso.ensure_bundle(self.data, self.plugin, self.user_skills, names, **kwargs)

    def bundle_skill(self, name):
        return read(self.data, "runtime", "bundle", ".claude", "skills", name, "SKILL.md")


class TestBundle(World):
    def test_builds_agents_and_skills_with_plain_names(self):
        result = self.ensure()
        bundle = result["path"]
        self.assertEqual(bundle, os.path.join(self.data, "runtime", "bundle"))
        self.assertTrue(result["rebuilt"])
        self.assertEqual(result["agents"], ["executor-back", "verifier"])
        self.assertEqual(sorted(os.listdir(os.path.join(bundle, ".claude", "agents"))), ["executor-back.md", "verifier.md"])
        self.assertEqual(sorted(os.listdir(os.path.join(bundle, ".claude", "skills"))), ["backend-dev", "orchestrator", "repo-catalog"])
        self.assertTrue(os.path.isfile(os.path.join(bundle, ".claude", "skills", "backend-dev", "references", "a.md")))

    def test_user_skill_wins_over_the_plugin_one(self):
        self.ensure()
        self.assertEqual(self.bundle_skill("repo-catalog"), "user repo-catalog")
        self.assertEqual(self.bundle_skill("orchestrator"), "plugin orchestrator")

    def test_missing_skills_are_skipped_not_an_error(self):
        result = self.ensure()
        self.assertEqual(result["skipped"], ["ghost"])
        self.assertNotIn("error", result)
        self.assertEqual(result["skills"], ["backend-dev", "orchestrator", "repo-catalog"])

    def test_signature_cache_does_not_rebuild_without_a_change(self):
        self.ensure()
        marker = os.path.join(self.data, "runtime", "bundle", "marker.txt")
        write(marker)
        iso.clear_memo()  # the in-process memo is off: only the stored signature decides
        again = self.ensure()
        self.assertFalse(again["rebuilt"])
        self.assertTrue(os.path.isfile(marker), "an unchanged bundle must not be rebuilt")

    def test_rebuilds_when_a_source_changes_or_the_names_change(self):
        self.ensure()
        write(os.path.join(self.user_skills, "backend-dev", "SKILL.md"), "user backend-dev changed and longer")
        self.assertTrue(self.ensure(use_memo=False)["rebuilt"])
        self.assertEqual(self.bundle_skill("backend-dev"), "user backend-dev changed and longer")
        write(os.path.join(self.user_skills, "ghost", "SKILL.md"), "now present")
        result = self.ensure(use_memo=False)
        self.assertTrue(result["rebuilt"])
        self.assertEqual(result["skipped"], [])

    def test_memo_makes_the_second_call_free(self):
        self.ensure()
        write(os.path.join(self.user_skills, "backend-dev", "SKILL.md"), "changed behind the memo")
        self.assertFalse(self.ensure()["rebuilt"], "within 30 s the memo answers without looking at the disk")
        self.assertTrue(self.ensure(use_memo=False)["rebuilt"])

    def test_swap_is_atomic_and_leaves_no_temp_folders(self):
        self.ensure()
        write(os.path.join(self.user_skills, "backend-dev", "SKILL.md"), "v2 with more text")
        seen = []
        real_replace = os.replace

        def spying_replace(source, target):
            seen.append((os.path.basename(source), os.path.basename(target)))
            return real_replace(source, target)

        os.replace = spying_replace
        try:
            self.assertTrue(self.ensure(use_memo=False)["rebuilt"])
        finally:
            os.replace = real_replace
        self.assertTrue(any(name.startswith("bundle.tmp-") and target == "bundle" for name, target in seen), seen)
        self.assertEqual(sorted(os.listdir(os.path.join(self.data, "runtime"))), ["bundle"])

    def test_failed_swap_keeps_the_old_bundle_and_reports(self):
        self.ensure()
        write(os.path.join(self.user_skills, "backend-dev", "SKILL.md"), "v2 with more text")
        real = iso.swap_in

        def refusing(temp, bundle):
            raise PermissionError("in use")

        iso.swap_in = refusing
        try:
            result = self.ensure(use_memo=False)
        finally:
            iso.swap_in = real
        self.assertIn("error", result)
        self.assertFalse(result["rebuilt"])
        self.assertEqual(self.bundle_skill("backend-dev"), "user backend-dev")
        self.assertEqual(sorted(os.listdir(os.path.join(self.data, "runtime"))), ["bundle"])


class TestAllowlist(World):
    def test_defaults_match_the_spec(self):
        allowlist = iso.skills_allowlist(None, {})
        self.assertTrue(GUARD_PROFILE_KEYS <= set(allowlist), "every profile name of the guard has a key")
        self.assertEqual(allowlist["verifier"], ["flow-validator", "playwright-cli", "playwright-trace", "playwright-component-testing", "verify-and-stop"])
        self.assertIn("investigate-first", allowlist["spec"])
        self.assertEqual(allowlist["orchestrator"][-2:], ["tlc-spec-driven", "repo-catalog"])
        self.assertEqual(allowlist["planning"], allowlist["systemmap"])
        self.assertEqual(allowlist["auto"], allowlist["orchestrator"])

    def test_keys_cover_the_guard_profiles(self):
        if not os.path.isfile(engine_env.GUARD_REAL):
            self.skipTest("guard.py not in this clone")
        text = read(engine_env.GUARD_REAL)
        start = text.index("PROFILES = {")
        end = text.index("\n}\n", start) + 3
        namespace = {}
        exec(text[start:end], namespace)  # only the dict literal
        self.assertTrue(set(namespace["PROFILES"]) <= set(iso.skills_allowlist(None, {})), set(namespace["PROFILES"]))

    def test_subproject_skills_go_to_spec_and_executor_only(self):
        repo = os.path.join(self.root, "repo")
        write(os.path.join(repo, ".specs", "project", "SUBPROJECTS.json"), json.dumps({
            "confirmed": True, "subprojects": [{"path": "api", "skill": "backend-dev"}, {"path": "web", "skill": "idata-guia-estilo"},
                                               {"path": "tools", "skill": "-"}, {"path": "seed", "skill": "seed-skill", "skip": True}]}))
        allowlist = iso.skills_allowlist(repo, {})
        self.assertIn("idata-guia-estilo", allowlist["spec"])
        self.assertIn("idata-guia-estilo", allowlist["executor"])
        self.assertNotIn("idata-guia-estilo", allowlist["verifier"])
        self.assertNotIn("idata-guia-estilo", allowlist["planning"])
        self.assertNotIn("-", allowlist["executor"])
        self.assertNotIn("seed-skill", allowlist["executor"])
        self.assertEqual(allowlist["executor"].count("backend-dev"), 1, "no duplicates")

    def test_repos_shape_of_subprojects_file_is_read_too(self):
        repo = os.path.join(self.root, "repo")
        write(os.path.join(repo, ".specs", "project", "SUBPROJECTS.json"), json.dumps({"repos": {"repo": {"projects": [{"path": "a", "skill": "custom-skill"}]}}}))
        self.assertIn("custom-skill", iso.skills_allowlist(repo, {})["executor"])

    def test_loop_extra_skills_reach_every_profile(self):
        allowlist = iso.skills_allowlist(None, {"loop_extra_skills": ["idata-guia-estilo", " "]})
        for profile in GUARD_PROFILE_KEYS | {"auto"}:
            self.assertIn("idata-guia-estilo", allowlist[profile])

    def test_allowlist_file_is_stable_and_project_specific_when_it_differs(self):
        self.set_env(LOOP_DATA_DIR=self.data)
        default = iso.skills_allowlist(None, {})
        path = iso.write_allowlist_file(self.data, default)
        self.assertEqual(path, os.path.join(self.data, "runtime", "skills-allowlist.json"))
        self.assertEqual(json.loads(read(path)), default)
        different = dict(default, executor=default["executor"] + ["only-here"])
        other = iso.write_allowlist_file(self.data, different)
        self.assertNotEqual(other, path)
        self.assertEqual(json.loads(read(path)), default, "the shared file is not overwritten")
        self.assertEqual(iso.write_allowlist_file(self.data, different), other)

    def test_isolation_enabled_only_false_turns_it_off(self):
        self.assertTrue(iso.isolation_enabled({}))
        self.assertTrue(iso.isolation_enabled({"loop_isolation": True}))
        self.assertTrue(iso.isolation_enabled({"loop_isolation": "no"}))
        self.assertFalse(iso.isolation_enabled({"loop_isolation": False}))

    def test_config_is_read_from_the_data_dir(self):
        self.set_env(LOOP_DATA_DIR=self.data)
        self.assertTrue(iso.isolation_enabled())
        write(os.path.join(self.data, "config.json"), json.dumps({"loop_isolation": False}))
        self.assertFalse(iso.isolation_enabled())


class TestSettings(World):
    GUARD = os.path.join("x", "plugin", "skills", "orchestrator", "hooks", "guard.py")

    def hook_command(self, settings):
        return json.loads(settings)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def test_guard_hook_with_profile_and_no_auto_mode(self):
        settings = iso.guard_settings(self.GUARD, "planning", {})
        self.assertEqual(self.hook_command(settings), 'python "%s" --profile planning' % os.path.abspath(self.GUARD).replace("\\", "/"))
        self.assertNotIn("autoMode", json.loads(settings))
        self.assertEqual(json.loads(settings)["hooks"]["PreToolUse"][0]["matcher"], "*")

    def test_auto_mode_is_copied_and_permissions_are_not(self):
        user = {"autoMode": {"allow": ["x"], "soft_deny": ["y"]}, "permissions": {"allow": ["Bash"]}, "hooks": {"Stop": []}}
        parsed = json.loads(iso.guard_settings(self.GUARD, "auto", user))
        self.assertEqual(parsed["autoMode"], user["autoMode"])
        self.assertEqual(set(parsed), {"hooks", "autoMode"})
        self.assertIn("--profile auto", parsed["hooks"]["PreToolUse"][0]["hooks"][0]["command"])

    def test_reads_the_user_settings_file_when_not_given(self):
        self.set_env(LOOP_CLAUDE_HOME=os.path.join(self.root, "home", ".claude"))
        write(os.path.join(self.root, "home", ".claude", "settings.json"), json.dumps({"autoMode": {"allow": ["z"]}}))
        self.assertEqual(json.loads(iso.guard_settings(self.GUARD, "spec"))["autoMode"], {"allow": ["z"]})

    def test_no_guard_and_no_auto_mode_gives_no_settings(self):
        self.assertEqual(iso.guard_settings("", "spec", {}), "")
        self.assertEqual(json.loads(iso.guard_settings("", "spec", {"autoMode": {}})), {"autoMode": {}})

    def test_argv_and_env_shapes(self):
        argv = iso.isolation_argv("/b", '{"a & b":1}')
        self.assertEqual(argv[:4], ["--setting-sources", "project,local", "--add-dir=/b", "--settings"])
        self.assertTrue(os.path.isfile(argv[4]), "the settings go in a file (a cmd.exe wrapper splits inline JSON on '&')")
        self.assertEqual(read(argv[4]), '{"a & b":1}')
        self.assertEqual(iso.isolation_argv("/b", ""), ["--setting-sources", "project,local", "--add-dir=/b"])
        self.assertEqual(iso.isolation_env("/f.json"), {"LOOP_ISOLATED": "1", "LOOP_SKILLS_FILE": "/f.json"})


CAVEMAN_HOOK = {"type": "command", "command": 'cat "C:/Users/x/.claude/Caveman-sessionstart.json"'}
OTHER_HOOK = {"type": "command", "command": "echo hello"}


def user_settings_with_caveman():
    return {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [OTHER_HOOK, CAVEMAN_HOOK]}, {"hooks": [OTHER_HOOK]}],
                      "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "caveman-pre"}]}]}}


class TestCavemanCarry(World):
    """CAV-01/02: the caveman SessionStart hooks join the isolated --settings only with `loop_caveman` on and isolation on."""
    GUARD = TestSettings.GUARD

    def configure(self, **config):
        self.set_env(LOOP_DATA_DIR=self.data)
        write(os.path.join(self.data, "config.json"), json.dumps(config))

    def test_find_keeps_only_caveman_session_start_hooks_case_insensitive(self):
        found = iso.find_caveman_hooks(user_settings_with_caveman())
        self.assertEqual(found, [{"matcher": "startup", "hooks": [CAVEMAN_HOOK]}])
        self.assertEqual(iso.find_caveman_hooks({}), [])
        self.assertEqual(iso.find_caveman_hooks({"hooks": {"SessionStart": [{"hooks": [OTHER_HOOK]}]}}), [])
        self.assertEqual(iso.find_caveman_hooks({"hooks": {"SessionStart": "bad"}}), [])

    def test_off_by_default_carries_nothing(self):
        self.configure()
        parsed = json.loads(iso.guard_settings(self.GUARD, "auto", user_settings_with_caveman()))
        self.assertEqual(list(parsed["hooks"]), ["PreToolUse"])

    def test_on_carries_the_hook_and_leaves_the_guard_untouched(self):
        self.configure(loop_caveman=True)
        off_guard = json.loads(iso.guard_settings(self.GUARD, "auto", {}))["hooks"]["PreToolUse"]
        parsed = json.loads(iso.guard_settings(self.GUARD, "auto", user_settings_with_caveman()))
        self.assertEqual(parsed["hooks"]["SessionStart"], [{"matcher": "startup", "hooks": [CAVEMAN_HOOK]}])
        self.assertEqual(parsed["hooks"]["PreToolUse"], off_guard, "the guard hook stays exactly as it was")

    def test_on_without_a_guard_still_carries_it(self):
        self.configure(loop_caveman=True)
        parsed = json.loads(iso.guard_settings("", "auto", user_settings_with_caveman()))
        self.assertEqual(list(parsed["hooks"]), ["SessionStart"])

    def test_on_but_not_installed_carries_nothing(self):
        self.configure(loop_caveman=True)
        self.assertEqual(iso.caveman_hooks_to_carry(user_settings={"hooks": {"SessionStart": [{"hooks": [OTHER_HOOK]}]}}), [])
        self.assertEqual(iso.guard_settings("", "auto", {}), "")

    def test_isolation_off_or_non_boolean_value_carries_nothing(self):
        self.configure(loop_caveman=True, loop_isolation=False)
        self.assertEqual(iso.caveman_hooks_to_carry(user_settings=user_settings_with_caveman()), [])
        self.configure(loop_caveman="yes")
        self.assertEqual(iso.caveman_hooks_to_carry(user_settings=user_settings_with_caveman()), [])

    def test_reads_the_user_settings_of_loop_claude_home(self):
        self.configure(loop_caveman=True)
        self.set_env(LOOP_CLAUDE_HOME=os.path.join(self.root, "home", ".claude"))
        write(os.path.join(self.root, "home", ".claude", "settings.json"), json.dumps(user_settings_with_caveman()))
        self.assertEqual(len(json.loads(iso.guard_settings(self.GUARD, "auto"))["hooks"]["SessionStart"]), 1)


class Opts:
    claude = "claude"
    permission_mode = "auto"
    budget = 5.0


ISOLATION_FLAGS = ("--setting-sources", "--settings")


class TestLoopArgv(LoopCase):
    """Loop turns: flags on, and byte-identical argv with `loop_isolation: false`."""

    def setUp(self):
        self.data = os.environ["LOOP_DATA_DIR"]
        self.config = os.path.join(self.data, "config.json")
        os.makedirs(self.data, exist_ok=True)
        self.had_config = os.path.exists(self.config)
        self.addCleanup(self.restore_config)
        iso.clear_memo()

    def restore_config(self):
        if not self.had_config and os.path.exists(self.config):
            os.remove(self.config)
        iso.clear_memo()

    def set_isolation(self, enabled):
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump({"loop_isolation": enabled}, handle)
        iso.clear_memo()

    def without_isolation(self, argv):
        out, skip = [], 0
        for item in argv:
            if skip:
                skip -= 1
            elif item in ISOLATION_FLAGS:
                skip = 1
            elif item.startswith("--add-dir=") and "bundle" in item:
                continue
            else:
                out.append(item)
        return out

    def test_unit_on_adds_flags_before_strict_mcp_and_off_is_the_old_argv(self):
        self.set_isolation(False)
        off = run_loop.claude_command(Opts(), "/orchestrator continue")
        self.assertFalse([item for item in off if item in ISOLATION_FLAGS])
        self.set_isolation(True)
        on = run_loop.claude_command(Opts(), "/orchestrator continue")
        self.assertEqual(on[on.index("--setting-sources") + 1], "project,local")
        bundle = [item for item in on if item.startswith("--add-dir=")][0]
        self.assertEqual(bundle, "--add-dir=%s" % os.path.join(self.data, "runtime", "bundle"))
        self.assertLess(on.index("--setting-sources"), on.index("--strict-mcp-config"))
        self.assertIn("--profile auto", read(on[on.index("--settings") + 1]))
        self.assertEqual(self.without_isolation(on), off, "isolation only adds flags; everything else stays byte for byte")

    def test_unit_env_carries_the_variables_only_when_on(self):
        self.set_isolation(True)
        env = run_loop.isolation_environment(self.data)
        self.assertEqual(env["LOOP_ISOLATED"], "1")
        self.assertTrue(os.path.isfile(env["LOOP_SKILLS_FILE"]))
        self.assertIn("executor", json.loads(read(env["LOOP_SKILLS_FILE"])))
        self.set_isolation(False)
        self.assertEqual(run_loop.isolation_environment(self.data), {})

    def run_logged(self, extra=None):
        scenario = dict({"features": {"a": {"paths": "apps/a"}}}, **(extra or {}))
        project = self.make(ROAD, scenario)
        log = project.scenario.replace("scenario.json", "argv.jsonl")
        with open(project.scenario, "w", encoding="utf-8") as handle:
            json.dump(dict(scenario, argv_log=log), handle)
        self.done(project.run("--wave", "1"), 0)
        with open(log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_loop_turns_and_the_correction_turn_carry_the_flags(self):
        self.set_isolation(True)
        argvs = self.run_logged({"forget_loop_json": True})
        self.assertTrue([argv for argv in argvs if "--resume" in argv], "a correction turn ran")
        for argv in argvs:
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local", argv)
            self.assertTrue([item for item in argv if item.startswith("--add-dir=") and "bundle" in item], argv)

    def run_turn_log(self, caveman, with_hook):
        """Runs one loop turn with the fake claude and returns the first turn record (`turns/001.json`)."""
        home = os.environ["LOOP_CLAUDE_HOME"]
        settings_path = os.path.join(home, "settings.json")
        self.addCleanup(lambda: os.path.exists(settings_path) and os.remove(settings_path))
        if os.path.exists(settings_path):
            os.remove(settings_path)
        if with_hook:
            write(settings_path, json.dumps(user_settings_with_caveman()))
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump({"loop_caveman": caveman}, handle)
        iso.clear_memo()
        scenario = {"features": {"a": {"paths": "apps/a"}}}
        project = self.make(ROAD, scenario)
        self.done(project.run("--wave", "1"), 0)
        with open(os.path.join(run_loop.loop_state.turns_dir(project.work), "001.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_turn_record_says_whether_caveman_was_on(self):
        self.assertIs(self.run_turn_log(True, True)["caveman"], True)

    def test_turn_record_caveman_false_when_off_or_not_installed(self):
        self.assertIs(self.run_turn_log(False, True)["caveman"], False)
        self.assertIs(self.run_turn_log(True, False)["caveman"], False)

    def test_kill_switch_off_sends_no_isolation_flag_on_any_turn(self):
        self.set_isolation(False)
        argvs = self.run_logged({"forget_loop_json": True})
        self.assertTrue(argvs)
        for argv in argvs:
            self.assertFalse([item for item in argv if item in ISOLATION_FLAGS or "bundle" in item], argv)


if __name__ == "__main__":
    unittest.main()


class TestCavemanSkillAllowed(unittest.TestCase):
    def test_caveman_on_adds_the_skill_to_every_profile_and_off_does_not(self):
        on = iso.skills_allowlist(None, {"loop_caveman": True})
        self.assertTrue(all("caveman" in skills for skills in on.values()))
        off = iso.skills_allowlist(None, {})
        self.assertFalse(any("caveman" in skills for skills in off.values()))
