#!/usr/bin/env python3
"""Tests of the 3 satellite tools of the orchestrator: rules.py (create a block rule), audit_deps.py (forgotten dependency) and
graph_view.py (progress drawing), plus how they are wired into the guard, the preflight and the notice; and the migration of
old `.specs/` layouts (loop_state.migrate_layout). No tokens, no network.

Run:  python -m unittest discover -s backend/tests/engine -t backend -v   (from the app root)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402  (engine on sys.path, temporary LOOP_DATA_DIR, LOOP_SKILL_DIR = fake skill with the copied guard)
LOOP = engine_env.ENGINE_DIR
import audit_deps  # noqa: E402
import loop_state  # noqa: E402
import graph_view  # noqa: E402
import notify  # noqa: E402
from test_loop import LoopCase, ROAD_BASIC, SCEN_BASIC  # noqa: E402

RULES = os.path.join(LOOP, "rules.py")
AUDIT = os.path.join(LOOP, "audit_deps.py")
LOOP_STATE = os.path.join(LOOP, "loop_state.py")
GUARD = engine_env.GUARD


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run_guard(profile, payload):
    return subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload), capture_output=True, text=True)


class TempRepo(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="satellite-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, ".specs", "project"))

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def project(self, name):
        return self.p(".specs", "project", name)

    def loop(self, name):
        return loop_state.loop_path(self.root, name)

    def roadmap(self, text):
        write(self.project("ROADMAP.md"), text)


class TestCreateBlockRule(TempRepo):
    def rules(self, command, *args):
        return subprocess.run([sys.executable, RULES, command, self.root, *args], capture_output=True, text=True, encoding="utf-8")

    def taxes_args(self, rule_id="R-04"):
        return ["--id", rule_id, "--phrase", "never edit the Taxes folder", "--rule", "Never edit Taxes", "--reason", "sensitive tax calculation",
                "--path", r"[\\/]Taxes[\\/]", "--block", "path:src/Taxes/Calc.cs", "--block", "path:Taxes/x.ts",
                "--pass", "path:src/Freight/Calc.cs", "--pass", "path:docs/guide.md"]

    def test_propose_tests_but_does_not_write(self):
        done = self.rules("propose", *self.taxes_args())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("PASSED", done.stdout)
        self.assertFalse(os.path.exists(self.loop("PERMISSIONS.json")))
        self.assertFalse(os.path.exists(self.loop("rules.tests.json")))

    def test_write_creates_the_three_files_and_the_guard_starts_blocking(self):
        done = self.rules("write", *self.taxes_args())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        perms = json.loads(read(self.loop("PERMISSIONS.json")))
        self.assertEqual(perms["deny_path_regex"], [r"[\\/]Taxes[\\/]"])
        tests = json.loads(read(self.loop("rules.tests.json")))
        self.assertEqual(tests["R-04"]["phrase"], "never edit the Taxes folder")
        self.assertEqual(tests["R-04"]["block"], ["path:src/Taxes/Calc.cs", "path:Taxes/x.ts"])
        rules_md = read(self.project("RULES.md"))
        self.assertIn("| R-04 |", rules_md)
        self.assertIn("never edit the Taxes folder", rules_md)
        payload = {"tool_name": "Edit", "tool_input": {"file_path": self.p("src", "Taxes", "x.cs")}, "cwd": self.root}
        blocked = run_guard("executor", payload)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("project-deny", blocked.stderr)
        self.assertEqual(self.rules("test").returncode, 0)
        self.assertIn("R-04", self.rules("list").stdout)

    def test_command_rule(self):
        done = self.rules("write", "--id", "R-01", "--phrase", "never call the tracking service", "--bash", r"curl\s.*tracking",
                          "--block", "bash:curl https://api.x/tracking/1", "--pass", "bash:curl https://api.x/orders/1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(read(self.loop("PERMISSIONS.json")))["deny_bash_regex"], [r"curl\s.*tracking"])

    def test_regex_too_wide_or_empty_is_refused(self):
        for wide in (".", ".*", "src", ""):
            done = self.rules("propose", "--id", "R-09", "--phrase", "x", "--path", wide, "--block", "path:a/b", "--pass", "path:c/d")
            self.assertEqual(done.returncode, 2, wide + done.stdout)
        self.assertFalse(os.path.exists(self.loop("PERMISSIONS.json")))

    def test_example_that_does_not_block_is_rejected_and_nothing_is_written(self):
        done = self.rules("write", "--id", "R-02", "--phrase", "never Foo", "--path", "Foo", "--block", "path:src/Bar/x.cs", "--pass", "path:docs/a.md")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("REJECTED", done.stdout)
        self.assertFalse(os.path.exists(self.loop("PERMISSIONS.json")))
        self.assertFalse(os.path.exists(self.project("RULES.md")))

    def test_without_the_user_phrase_nothing_is_written(self):
        args = [a for a in self.taxes_args()]
        args[args.index("--phrase") + 1] = ""
        done = self.rules("write", *args)
        self.assertEqual(done.returncode, 2)
        self.assertFalse(os.path.exists(self.loop("PERMISSIONS.json")))

    def test_without_examples_there_is_no_proof(self):
        done = self.rules("propose", "--id", "R-05", "--phrase", "x", "--path", "Foo", "--block", "path:a/Foo/x")
        self.assertEqual(done.returncode, 2)

    def test_new_rule_that_breaks_an_old_rule_is_rejected(self):
        self.assertEqual(self.rules("write", *self.taxes_args("R-01")).returncode, 0)
        done = self.rules("write", "--id", "R-02", "--phrase", "never the Calc file", "--path", "Calc", "--block", "path:Calc.cs", "--pass", "path:README.md")
        self.assertEqual(done.returncode, 1, done.stdout)  # R-01 requires src/Freight/Calc.cs to pass
        self.assertIn("BREAKS older rules", done.stdout)
        self.assertNotIn("R-02", read(self.loop("rules.tests.json")))

    def test_repeated_id_is_refused(self):
        self.assertEqual(self.rules("write", *self.taxes_args("R-01")).returncode, 0)
        self.assertEqual(self.rules("propose", *self.taxes_args("R-01")).returncode, 2)

    def test_test_reports_a_rule_that_stopped_working(self):
        self.assertEqual(self.rules("write", *self.taxes_args()).returncode, 0)
        perms = json.loads(read(self.loop("PERMISSIONS.json")))
        perms["deny_path_regex"] = []  # someone deleted the regex
        write(self.loop("PERMISSIONS.json"), json.dumps(perms))
        done = self.rules("test")
        self.assertEqual(done.returncode, 1)
        self.assertIn("R-04", done.stdout)

    def test_rules_sit_next_to_the_ones_already_in_rules_md(self):
        write(self.project("RULES.md"), "# Limiting rules\n\n| ID | Rule | Why | How to enforce | Origin |\n|----|------|-----|----------------|--------|\n| R-01 | old | x | y | z |\n\n## Environment limits\n- port 80\n")
        self.assertEqual(self.rules("write", *self.taxes_args("R-02")).returncode, 0)
        text = read(self.project("RULES.md"))
        self.assertLess(text.index("| R-01 |"), text.index("| R-02 |"))
        self.assertLess(text.index("| R-02 |"), text.index("## Environment limits"))

    def test_only_the_orchestrator_writes_rules(self):
        def code(profile, command):
            return run_guard(profile, {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.root}).returncode
        self.assertEqual(code("executor", "python engine/rules.py write . --id R-01"), 2)
        self.assertEqual(code("spec", "python engine/rules.py write . --id R-01"), 2)
        self.assertEqual(code("executor", "python app/harness.py rules write . --id R-01"), 2)
        self.assertEqual(code("orchestrator", "python engine/rules.py write . --id R-01"), 0)
        self.assertEqual(code("executor", "python engine/rules.py test ."), 0)  # reading/testing is free

    def test_subagent_does_not_write_the_new_state_files(self):
        for path in (self.loop("DEPENDENCIES.md"), self.loop("GRAPH.md"), self.loop("rules.tests.json"), self.project("rules.tests.json")):
            name = os.path.relpath(path, self.root)
            done = run_guard("executor", {"tool_name": "Edit", "tool_input": {"file_path": path}, "cwd": self.root})
            self.assertEqual(done.returncode, 2, name)


class TestAuditDependencies(TempRepo):
    ROAD = "## Stage 1\n- [api] status=building scope=Small kind=back paths=apps/api\n- [screen] status=approved scope=Small kind=front paths=apps/web\n"

    def audit(self):
        return audit_deps.audit(self.root)

    def pairs(self, result):
        return {(s["feature"], s["depends_on"]): s for s in result["suggestions"]}

    def run_audit(self, flag):
        return subprocess.run([sys.executable, AUDIT, self.root, flag], capture_output=True, text=True, encoding="utf-8")

    def test_typescript_relative_import_of_another_feature(self):
        self.roadmap(self.ROAD)
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        write(self.p("apps", "api", "orders.ts"), "export class Order {}\n")
        found = self.pairs(self.audit())
        self.assertIn(("screen", "api"), found)
        self.assertEqual(found[("screen", "api")]["type"], "import")
        self.assertIn("apps/web/screen.ts:1", found[("screen", "api")]["evidence"][0])

    def test_typescript_tsconfig_alias(self):
        self.roadmap(self.ROAD)
        write(self.p("apps", "web", "tsconfig.json"), '{ // comment\n "compilerOptions": {"baseUrl": ".", "paths": {"@api/*": ["../api/*"],},},\n}')
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '@api/orders';\n")
        self.assertIn(("screen", "api"), self.pairs(self.audit()))

    def test_csharp_using_by_namespace(self):
        self.roadmap("## Stage 1\n- [api] status=building paths=src/Orders\n- [cart] status=approved paths=src/Cart\n")
        write(self.p("src", "Orders", "Order.cs"), "namespace Shop.Orders\n{\n    public class Order {}\n}\n")
        write(self.p("src", "Cart", "Cart.cs"), "using System;\nusing Shop.Orders;\nnamespace Shop.Cart { }\n")
        self.assertIn(("cart", "api"), self.pairs(self.audit()))

    def test_csharp_project_reference(self):
        self.roadmap("## Stage 1\n- [api] status=building paths=src/Orders\n- [cart] status=approved paths=src/Cart\n")
        write(self.p("src", "Cart", "Cart.csproj"), '<Project><ItemGroup><ProjectReference Include="..\\Orders\\Orders.csproj" /></ItemGroup></Project>')
        self.assertIn(("cart", "api"), self.pairs(self.audit()))

    def test_python_import_of_a_repo_module(self):
        self.roadmap("## Stage 1\n- [core] status=building paths=libs/core\n- [app] status=approved paths=apps/app\n")
        write(self.p("libs", "core", "__init__.py"), "")
        write(self.p("apps", "app", "main.py"), "from libs.core import everything\n")
        self.assertIn(("app", "core"), self.pairs(self.audit()))

    def test_spec_text_mentions_the_other_feature(self):
        self.roadmap("## Stage 1\n- [checkout-pay] status=pending paths=apps/pay\n- [screen] status=pending paths=apps/web\n")
        write(self.p(".specs", "features", "screen", "spec.md"), "# Screen\n\n## Requirements\nThe screen calls the feature `checkout-pay` to pay.\n")
        found = self.pairs(self.audit())
        self.assertIn(("screen", "checkout-pay"), found)
        self.assertEqual(found[("screen", "checkout-pay")]["type"], "text")

    def test_mention_under_out_of_scope_does_not_count(self):
        self.roadmap("## Stage 1\n- [checkout-pay] status=pending paths=apps/pay\n- [screen] status=pending paths=apps/web\n")
        write(self.p(".specs", "features", "screen", "spec.md"), "# Screen\n\n## Out of scope\nDoes not do `checkout-pay`.\n")
        self.assertEqual(self.audit()["suggestions"], [])

    def test_already_verified_feature_generates_no_suggestion(self):
        self.roadmap(self.ROAD.replace("api] status=building", "api] status=verified"))
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        self.assertEqual(self.audit()["suggestions"], [])

    def test_declared_dependency_is_not_suggested_again(self):
        self.roadmap(self.ROAD.replace("paths=apps/web", "paths=apps/web deps=api"))
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        result = self.audit()
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["review"], [])  # has evidence, so it is not a leftover

    def test_deps_without_evidence_go_to_review(self):
        self.roadmap(self.ROAD.replace("paths=apps/web", "paths=apps/web deps=api"))
        write(self.p("apps", "web", "screen.ts"), "export const x = 1;\n")
        self.assertEqual(self.audit()["review"], [{"feature": "screen", "depends_on": "api"}])

    def test_suggestion_that_closes_a_cycle_is_not_applied(self):
        self.roadmap(self.ROAD.replace("paths=apps/api", "paths=apps/api deps=screen"))
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        result = self.audit()
        self.assertTrue(result["suggestions"][0]["cycle"])
        done = self.run_audit("--apply")
        self.assertNotIn("APPLIED", done.stdout)
        self.assertNotIn("deps=screen,", read(self.project("ROADMAP.md")))
        self.assertIn("[cycle] screen -> api", read(self.loop("DEPENDENCIES.md")))

    def test_apply_writes_deps_to_the_roadmap_and_notes_them_in_dependencies_md(self):
        self.roadmap(self.ROAD)
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        done = self.run_audit("--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("APPLIED: screen deps+=api", done.stdout)
        features, errors = loop_state.parse_roadmap(read(self.project("ROADMAP.md")))
        self.assertEqual(features["screen"]["deps"], "api")
        self.assertEqual(errors + loop_state.check_fields(features), [])
        notes = read(self.loop("DEPENDENCIES.md"))
        self.assertIn("[applied] screen -> api | import", notes)
        self.assertIn("apps/web/screen.ts:1", notes)
        again = self.run_audit("--write")
        self.assertIn("0 deps suggestion", again.stdout)
        self.assertIn("[applied] screen -> api", read(self.loop("DEPENDENCIES.md")))  # decision preserved

    def test_apply_appends_to_existing_deps(self):
        self.roadmap("## Stage 1\n- [api] status=building paths=apps/api\n- [base] status=building paths=apps/base\n- [screen] status=approved paths=apps/web deps=base\n")
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        self.run_audit("--apply")
        self.assertEqual(loop_state.parse_roadmap(read(self.project("ROADMAP.md")))[0]["screen"]["deps"], "base,api")

    def test_suggestion_refused_by_the_user_does_not_come_back(self):
        self.roadmap(self.ROAD)
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        self.run_audit("--write")
        notes = read(self.loop("DEPENDENCIES.md"))
        self.assertIn("[pending] screen -> api", notes)
        write(self.loop("DEPENDENCIES.md"), notes.replace("[pending] screen -> api", "[refused] screen -> api"))
        done = self.run_audit("--apply")
        self.assertNotIn("APPLIED", done.stdout)
        self.assertNotIn("deps=", read(self.project("ROADMAP.md")))
        self.assertIn("[refused] screen -> api", read(self.loop("DEPENDENCIES.md")))

    def test_apply_does_not_write_if_the_roadmap_would_become_invalid(self):
        self.roadmap(self.ROAD)
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        original = read(self.project("ROADMAP.md"))
        result = self.audit()
        result["suggestions"][0]["depends_on"] = "ghost"  # nonexistent slug: the grammar refuses it
        applied, problems = audit_deps.apply_to_roadmap(self.root, result, {})
        self.assertEqual(applied, [])
        self.assertTrue(problems)
        self.assertEqual(read(self.project("ROADMAP.md")), original)


class TestProgressGraph(TempRepo):
    ROAD = ("## Stage 1\n- [api] status=verified paths=a\n- [ui] status=verified paths=b\n- [coupon] status=blocked tries=2 paths=c\n"
            "## Stage 2\n- [screen] status=building paths=d deps=api,ui,dir:packages/ui\n- [end] status=pending deps=coupon\n")

    def features(self):
        self.roadmap(self.ROAD)
        return graph_view.load(self.root)

    def test_text_shows_state_dependency_and_stuck_feature(self):
        text = graph_view.ascii_graph(self.features())
        self.assertIn("Stage 1  (3/3 closed)", text)
        self.assertIn("[x] api", text)
        self.assertIn("[!] coupon", text)
        self.assertIn("(2 failures)", text)
        self.assertIn("[>] screen", text)
        self.assertIn("<- api(done) ui(done) dir:packages/ui", text)
        self.assertIn("STUCK on: coupon", text)
        self.assertIn("Legend", text)

    def test_short_text_fits_a_size_limited_channel(self):
        features = self.features()
        line = graph_view.short(features, 300)
        self.assertLessEqual(len(line), 300)
        self.assertIn("S1:", line)
        self.assertLessEqual(len(graph_view.short(features, 60)), 60)
        self.assertNotIn("\n", line)

    def test_mermaid_has_dependency_arrows_and_status_colors(self):
        diagram = graph_view.mermaid(self.features())
        self.assertTrue(diagram.startswith("flowchart LR"))
        self.assertIn("n_api --> n_screen", diagram)
        self.assertIn("n_coupon --> n_end", diagram)
        self.assertIn(":::blocked", diagram)
        self.assertIn('subgraph S1["Stage 1"]', diagram)
        self.assertIn("classDef verified", diagram)

    def test_write_creates_graph_md_in_the_loop_folder(self):
        self.features()
        target = graph_view.write(self.root)
        self.assertTrue(target.endswith(os.path.join(".specs", "project", "loop", "GRAPH.md")), target)
        text = read(target)
        self.assertIn("```mermaid", text)
        self.assertIn("[x] api", text)

    def test_empty_roadmap_does_not_break(self):
        self.assertEqual(graph_view.ascii_graph(graph_view.load(self.root)), "(ROADMAP has no features)")

    def test_notice_carries_the_drawing_and_the_pr(self):
        ctx = {"event": "done", "project": "p", "graph": graph_view.ascii_graph(self.features()), "graph_short": "S1: api done",
               "pr": "https://github.com/example-org/x/pull/7", "pr_status": "PR created", "features": [("api", "verified")], "script": "r.py", "repo": "r", "flags": ""}
        _, full = notify.build(ctx)
        _, html = notify.build_html(ctx)
        self.assertIn("PROGRESS", full)
        self.assertIn("[x] api", full)
        self.assertIn("pull/7", full)
        self.assertIn("Progress", html)
        self.assertIn("pull/7", html)
        ctx["event"] = "awaiting_user"
        ctx["reason"] = "decide"
        _, short = notify.build_short(ctx)
        self.assertLessEqual(len(short), 1000)
        self.assertIn("S1: api done", short)


class TestLoopIntegration(LoopCase):
    def test_preflight_fails_if_a_block_rule_stopped_working(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        done = subprocess.run([sys.executable, RULES, "write", project.repo, "--id", "R-01", "--phrase", "never edit Taxes", "--path", r"[\\/]Taxes[\\/]",
                               "--block", "path:src/Taxes/x.cs", "--pass", "path:src/Freight/x.cs"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.done(project.run("--check"), 0)
        perms_path = os.path.join(project.repo, ".specs", "project", "loop", "json", "PERMISSIONS.json")
        write(perms_path, json.dumps({"allow": [], "deny_path_regex": []}))
        result = project.run("--check")
        self.done(result, 1)
        self.assertIn("a project block rule stopped working", result.stdout)

    def test_preflight_warns_about_a_forgotten_dependency(self):
        road = "## Stage 1\n- [api] status=pending\n- [screen] status=pending\n"
        project = self.make(road, {"features": {}})
        write(os.path.join(project.repo, ".specs", "project", "ROADMAP.md"), "## Stage 1\n- [api] status=approved paths=apps/api\n- [screen] status=approved paths=apps/web\n")
        write(os.path.join(project.repo, "apps", "web", "screen.ts"), "import { P } from '../api/orders';\n")
        result = project.run("--check")
        self.done(result, 0)
        self.assertIn("forgotten dependency(ies)", result.stdout)

    def test_script_applies_deps_before_choosing_the_wave(self):
        import run_loop
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        specs = os.path.join(project.repo, ".specs", "project")
        write(os.path.join(specs, "ROADMAP.md"), "## Stage 1\n- [api] status=approved paths=apps/api\n- [screen] status=approved paths=apps/web\n")
        write(os.path.join(project.repo, "apps", "web", "screen.ts"), "import { P } from '../api/orders';\n")
        applied = run_loop.audit_before_turn(project.repo)
        self.assertEqual(applied, ["screen deps+=api"])
        next_work = loop_state.next_work(loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0], 2, headless=True)
        self.assertEqual(next_work["slugs"], ["api"], "screen waits for api: it does not enter the same wave")

    def test_graph_is_rewritten_every_turn_and_goes_in_the_summary(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run("--wave", "2"), 0)
        graph = read(os.path.join(project.work, ".specs", "project", "loop", "GRAPH.md"))
        for slug in loop_state.parse_roadmap(ROAD_BASIC)[0]:
            self.assertIn("[x] " + slug, graph)
        summary = read(os.path.join(project.summary, sorted(os.listdir(project.summary))[0]))
        self.assertIn("Progress by feature", summary)
        self.assertIn("flowchart LR", summary)


class TestLoopFolderGuard(TempRepo):
    """The guard keeps reading a rule from the flat plugin layout until it is migrated, and protects the whole loop folder."""

    def test_guard_reads_the_rule_from_the_flat_layout_until_migrated(self):
        write(self.project("PERMISSIONS.json"), json.dumps({"deny_bash_regex": ["forbidden"]}))
        done = run_guard("executor", {"tool_name": "Bash", "tool_input": {"command": "echo forbidden"}, "cwd": self.root})
        self.assertEqual(done.returncode, 2, done.stderr)

    def test_guard_reads_the_rule_from_the_new_layout(self):
        write(self.loop("PERMISSIONS.json"), json.dumps({"deny_bash_regex": ["forbidden"]}))
        done = run_guard("executor", {"tool_name": "Bash", "tool_input": {"command": "echo forbidden"}, "cwd": self.root})
        self.assertEqual(done.returncode, 2, done.stderr)

    def test_guard_protects_the_whole_loop_folder(self):
        for name in ("guard.log", "LOOP.json", "anything.json", "ANSWERS.md"):
            done = run_guard("executor", {"tool_name": "Edit", "tool_input": {"file_path": self.loop(name)}, "cwd": self.root})
            self.assertEqual(done.returncode, 2, name)


class TestMigrateLayout(TempRepo):
    """loop_state.migrate_layout: old layouts (the Portuguese app layout, its older flat variants and the flat public plugin
    layout) move to .specs/project/loop/. This is the only test that names the legacy (Portuguese) files: they are the literal
    names found on disk in projects written by older versions."""

    def loop_dir(self, *parts):
        return self.p(".specs", "project", "loop", *parts)

    def assert_files(self, *relatives):
        for relative in relatives:
            self.assertTrue(os.path.isfile(self.loop_dir(*relative)), relative)

    def test_portuguese_loop_script_layout_moves_with_new_names(self):
        old = self.p(".specs", "project", "loop_script")
        write(os.path.join(old, "RESPOSTAS.md"), "answers")
        write(os.path.join(old, "VALIDAR.md"), "validate")
        write(os.path.join(old, "json", "ESCOPO.json"), "{}")
        write(os.path.join(old, "json", "PERMISSOES.json"), "{}")
        write(os.path.join(old, "json", "ROADMAP-APROVADO.json"), "{}")
        write(os.path.join(old, "interno", "voltas", "001.json"), "{}")
        write(os.path.join(old, "interno", "acoes.log"), "{}")
        write(os.path.join(old, "interno", "objetivo-roadmap.md"), "goal")
        write(os.path.join(old, "interno", "snapshots", "001", "RESPOSTAS.md"), "snapshot answers")
        moved = loop_state.migrate_layout(self.root)
        self.assertTrue(moved)
        self.assert_files(("ANSWERS.md",), ("VALIDATE.md",), ("json", "SCOPE.json"), ("json", "PERMISSIONS.json"),
                          ("json", "ROADMAP-APPROVED.json"), ("internal", "turns", "001.json"), ("internal", "actions.log"),
                          ("internal", "roadmap-goal.md"), ("internal", "snapshots", "001", "ANSWERS.md"))
        self.assertEqual(read(self.loop_dir("ANSWERS.md")), "answers")
        self.assertFalse(os.path.exists(old), "the emptied loop_script folder is removed")
        self.assertEqual(loop_state.migrate_layout(self.root), [], "idempotent")

    def test_flat_public_plugin_layout_moves_to_the_loop_folder(self):
        write(self.project("LOOP.json"), "{}")
        write(self.project("ANSWERS.md"), "answers")
        write(self.project("PERMISSIONS.json"), "{}")
        logs = self.p(".specs", "loop-logs")
        write(os.path.join(logs, "001.json"), "{}")
        write(os.path.join(logs, "meta.json"), "{}")
        write(os.path.join(logs, "NOTICE.txt"), "notice")
        write(os.path.join(logs, "delivery.json"), "{}")
        write(os.path.join(logs, "snapshots", "001", "snapshot.json"), "{}")
        loop_state.migrate_layout(self.root)
        self.assert_files(("json", "LOOP.json"), ("ANSWERS.md",), ("json", "PERMISSIONS.json"), ("internal", "turns", "001.json"),
                          ("json", "meta.json"), ("NOTICE.txt",), ("json", "delivery.json"), ("internal", "snapshots", "001", "snapshot.json"))
        self.assertFalse(os.path.exists(logs))
        self.assertFalse(os.path.exists(self.project("LOOP.json")))
        self.assertEqual(loop_state.migrate_layout(self.root), [])

    def test_older_flat_files_and_project_files_with_old_names(self):
        write(self.project("RESPOSTAS.md"), "answers")
        write(self.project("PERMISSOES.json"), "{}")
        write(self.project("REGRAS.md"), "# rules")
        write(self.project("TESTE-AMBIENTE.md"), "# environment")
        write(self.project(os.path.join("phases", "etapa-01.md")), "# stage 1")
        write(self.p(".specs", "project", "loop", "LOOP.json"), "{}")  # an older flat project/loop/ folder
        write(self.p(".specs", "project", "loop", "voltas", "002.json"), "{}")
        write(self.p("CLAUDE.md"), "# Project\n@.specs/project/REGRAS.md\nThe file REGRAS.md is mentioned in prose.\n")
        moved = loop_state.migrate_layout(self.root)
        self.assertIn("CLAUDE.md", moved)
        self.assert_files(("ANSWERS.md",), ("json", "PERMISSIONS.json"), ("json", "LOOP.json"), ("internal", "turns", "002.json"))
        self.assertEqual(read(self.project("RULES.md")), "# rules")
        self.assertTrue(os.path.isfile(self.project("TEST-ENVIRONMENT.md")))
        self.assertTrue(os.path.isfile(self.project(os.path.join("phases", "stage-01.md"))))
        self.assertFalse(os.path.exists(self.project("REGRAS.md")))
        self.assertFalse(os.path.exists(self.project(os.path.join("phases", "etapa-01.md"))))
        claude = read(self.p("CLAUDE.md"))
        self.assertIn("\n@.specs/project/RULES.md\n", claude)
        self.assertIn("The file REGRAS.md is mentioned in prose.", claude, "only the import line changes")
        self.assertEqual(loop_state.migrate_layout(self.root), [])

    def test_never_overwrites_the_destination_and_keeps_the_source(self):
        write(self.project("LOOP.json"), '{"old": true}')
        write(self.loop("LOOP.json"), '{"new": true}')
        self.assertEqual(loop_state.migrate_layout(self.root), [])
        self.assertEqual(read(self.loop("LOOP.json")), '{"new": true}')
        self.assertEqual(read(self.project("LOOP.json")), '{"old": true}')

    def test_cli_migrate_prints_what_moved_and_then_nothing(self):
        write(self.project("ANSWERS.md"), "answers")
        first = subprocess.run([sys.executable, LOOP_STATE, "migrate", self.root], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(first.stdout.startswith("moved: "), first.stdout)
        second = subprocess.run([sys.executable, LOOP_STATE, "migrate", self.root], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(second.stdout.strip(), "nothing to migrate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
