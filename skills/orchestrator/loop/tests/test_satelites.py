#!/usr/bin/env python3
"""Tests of the 3 satellite tools of the orchestrator: rules.py (create a block rule), audit_deps.py (forgotten dependency) and
graph_view.py (progress drawing), plus how they are wired into the guard, the pre-flight and the notice. No tokens, no network.

Run (from skills/orchestrator/loop):  python -I -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LOOP = os.path.dirname(HERE)
SKILL = os.path.dirname(LOOP)
sys.path.insert(0, LOOP)
sys.path.insert(0, HERE)
import audit_deps  # noqa: E402
import loop_state  # noqa: E402
import graph_view  # noqa: E402
import notify  # noqa: E402
from test_loop import LoopCase, ROAD_BASIC, SCEN_BASIC  # noqa: E402

RULES = os.path.join(LOOP, "rules.py")
AUDIT = os.path.join(LOOP, "audit_deps.py")
GUARD = os.path.join(SKILL, "hooks", "guard.py")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TempRepo(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="satellite-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, ".specs", "project"))

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def project(self, name):
        return self.p(".specs", "project", name)

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
        self.assertFalse(os.path.exists(self.project("PERMISSIONS.json")))
        self.assertFalse(os.path.exists(self.project("rules.tests.json")))

    def test_write_creates_the_three_files_and_the_guard_starts_blocking(self):
        done = self.rules("write", *self.taxes_args())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        perms = json.loads(read(self.project("PERMISSIONS.json")))
        self.assertEqual(perms["deny_path_regex"], [r"[\\/]Taxes[\\/]"])
        tests = json.loads(read(self.project("rules.tests.json")))
        self.assertEqual(tests["R-04"]["phrase"], "never edit the Taxes folder")
        rules_md = read(self.project("RULES.md"))
        self.assertIn("| R-04 |", rules_md)
        self.assertIn("never edit the Taxes folder", rules_md)
        payload = {"tool_name": "Edit", "tool_input": {"file_path": self.p("src", "Taxes", "x.cs")}, "cwd": self.root}
        blocked = subprocess.run([sys.executable, GUARD, "--profile", "executor"], input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("project-deny", blocked.stderr)
        self.assertIn("BLOCKED by the orchestrator guard", blocked.stderr)
        self.assertEqual(self.rules("test").returncode, 0)
        self.assertIn("R-04", self.rules("list").stdout)

    def test_command_rule(self):
        done = self.rules("write", "--id", "R-01", "--phrase", "never call the tracking service", "--bash", r"curl\s.*tracking",
                          "--block", "bash:curl https://api.x/tracking/1", "--pass", "bash:curl https://api.x/orders/1")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(read(self.project("PERMISSIONS.json")))["deny_bash_regex"], [r"curl\s.*tracking"])

    def test_regex_too_wide_or_empty_is_refused(self):
        for wide in (".", ".*", "src", ""):
            done = self.rules("propose", "--id", "R-09", "--phrase", "x", "--path", wide, "--block", "path:a/b", "--pass", "path:c/d")
            self.assertEqual(done.returncode, 2, wide + done.stdout)
        self.assertFalse(os.path.exists(self.project("PERMISSIONS.json")))

    def test_example_that_does_not_block_is_rejected_and_nothing_is_written(self):
        done = self.rules("write", "--id", "R-02", "--phrase", "never Foo", "--path", "Foo", "--block", "path:src/Bar/x.cs", "--pass", "path:docs/a.md")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("REJECTED", done.stdout)
        self.assertFalse(os.path.exists(self.project("PERMISSIONS.json")))
        self.assertFalse(os.path.exists(self.project("RULES.md")))

    def test_without_the_user_phrase_nothing_is_written(self):
        args = [a for a in self.taxes_args()]
        args[args.index("--phrase") + 1] = ""
        done = self.rules("write", *args)
        self.assertEqual(done.returncode, 2)
        self.assertFalse(os.path.exists(self.project("PERMISSIONS.json")))

    def test_without_examples_there_is_no_proof(self):
        done = self.rules("propose", "--id", "R-05", "--phrase", "x", "--path", "Foo", "--block", "path:a/Foo/x")
        self.assertEqual(done.returncode, 2)

    def test_new_rule_that_breaks_an_old_rule_is_rejected(self):
        self.assertEqual(self.rules("write", *self.taxes_args("R-01")).returncode, 0)
        done = self.rules("write", "--id", "R-02", "--phrase", "never the Calc file", "--path", "Calc", "--block", "path:Calc.cs", "--pass", "path:README.md")
        self.assertEqual(done.returncode, 1, done.stdout)  # R-01 requires src/Freight/Calc.cs to pass
        self.assertIn("BREAKS older rules", done.stdout)
        self.assertNotIn("R-02", read(self.project("rules.tests.json")))

    def test_repeated_id_is_refused(self):
        self.assertEqual(self.rules("write", *self.taxes_args("R-01")).returncode, 0)
        self.assertEqual(self.rules("propose", *self.taxes_args("R-01")).returncode, 2)

    def test_test_reports_a_rule_that_stopped_working(self):
        self.assertEqual(self.rules("write", *self.taxes_args()).returncode, 0)
        perms = json.loads(read(self.project("PERMISSIONS.json")))
        perms["deny_path_regex"] = []  # someone deleted the regex
        write(self.project("PERMISSIONS.json"), json.dumps(perms))
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
            payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.root}
            return subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload), capture_output=True, text=True).returncode
        self.assertEqual(code("executor", "python loop/rules.py write . --id R-01"), 2)
        self.assertEqual(code("spec", "python loop/rules.py write . --id R-01"), 2)
        self.assertEqual(code("orchestrator", "python loop/rules.py write . --id R-01"), 0)
        self.assertEqual(code("executor", "python loop/rules.py test ."), 0)  # reading/testing is free

    def test_subagent_does_not_write_the_new_state_files(self):
        for name in ("DEPENDENCIES.md", "GRAPH.md", "rules.tests.json"):
            payload = {"tool_name": "Edit", "tool_input": {"file_path": self.project(name)}, "cwd": self.root}
            done = subprocess.run([sys.executable, GUARD, "--profile", "executor"], input=json.dumps(payload), capture_output=True, text=True)
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
        self.assertIn("[cycle] screen -> api", read(self.project("DEPENDENCIES.md")))

    def test_apply_writes_deps_to_the_roadmap_and_notes_them_in_dependencies_md(self):
        self.roadmap(self.ROAD)
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        done = self.run_audit("--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("APPLIED: screen deps+=api", done.stdout)
        features, errors = loop_state.parse_roadmap(read(self.project("ROADMAP.md")))
        self.assertEqual(features["screen"]["deps"], "api")
        self.assertEqual(errors + loop_state.check_fields(features), [])
        notes = read(self.project("DEPENDENCIES.md"))
        self.assertIn("[applied] screen -> api | import", notes)
        self.assertIn("apps/web/screen.ts:1", notes)
        again = self.run_audit("--write")
        self.assertIn("0 deps suggestion", again.stdout)
        self.assertIn("[applied] screen -> api", read(self.project("DEPENDENCIES.md")))  # decision preserved

    def test_apply_appends_to_existing_deps(self):
        self.roadmap("## Stage 1\n- [api] status=building paths=apps/api\n- [base] status=building paths=apps/base\n- [screen] status=approved paths=apps/web deps=base\n")
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        self.run_audit("--apply")
        self.assertEqual(loop_state.parse_roadmap(read(self.project("ROADMAP.md")))[0]["screen"]["deps"], "base,api")

    def test_suggestion_refused_by_the_user_does_not_come_back(self):
        self.roadmap(self.ROAD)
        write(self.p("apps", "web", "screen.ts"), "import { Order } from '../api/orders';\n")
        self.run_audit("--write")
        notes = read(self.project("DEPENDENCIES.md"))
        self.assertIn("[pending] screen -> api", notes)
        write(self.project("DEPENDENCIES.md"), notes.replace("[pending] screen -> api", "[refused] screen -> api"))
        done = self.run_audit("--apply")
        self.assertNotIn("APPLIED", done.stdout)
        self.assertNotIn("deps=", read(self.project("ROADMAP.md")))
        self.assertIn("[refused] screen -> api", read(self.project("DEPENDENCIES.md")))

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
        self.assertIn("classDef verified", diagram)

    def test_write_creates_graph_md(self):
        self.features()
        target = graph_view.write(self.root)
        self.assertTrue(target.endswith(os.path.join(".specs", "project", "GRAPH.md")))
        text = read(target)
        self.assertIn("```mermaid", text)
        self.assertIn("[x] api", text)

    def test_empty_roadmap_does_not_break(self):
        self.assertEqual(graph_view.ascii_graph(graph_view.load(self.root)), "(ROADMAP has no features)")

    def test_notice_carries_the_drawing_and_the_pr(self):
        ctx = {"event": "done", "project": "p", "graph": graph_view.ascii_graph(self.features()),
               "pr": "https://github.com/example-org/x/pull/7", "pr_status": "PR created", "features": [("api", "verified")], "script": "r.py", "repo": "r", "flags": ""}
        _, full = notify.build(ctx)
        _, html = notify.build_html(ctx)
        self.assertIn("PROGRESS", full)
        self.assertIn("[x] api", full)
        self.assertIn("pull/7", full)
        self.assertIn("Progress", html)
        self.assertIn("pull/7", html)


class TestLoopIntegration(LoopCase):
    def test_pre_flight_fails_if_a_block_rule_stopped_working(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        done = subprocess.run([sys.executable, RULES, "write", project.repo, "--id", "R-01", "--phrase", "never edit Taxes", "--path", r"[\\/]Taxes[\\/]",
                               "--block", "path:src/Taxes/x.cs", "--pass", "path:src/Freight/x.cs"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.done(project.run("--check"), 0)
        perms_path = os.path.join(project.repo, ".specs", "project", "PERMISSIONS.json")
        write(perms_path, json.dumps({"allow": [], "deny_path_regex": []}))
        result = project.run("--check")
        self.done(result, 1)
        self.assertIn("a project block rule stopped working", result.stdout)

    def test_pre_flight_warns_about_a_forgotten_dependency(self):
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
        graph = read(os.path.join(project.work, ".specs", "project", "GRAPH.md"))
        self.assertIn("[x] api", graph)
        self.assertIn("[x] screen", graph)
        summary = open(os.path.join(project.summary, sorted(os.listdir(project.summary))[0]), encoding="utf-8").read()
        self.assertIn("Progress by feature", summary)
        self.assertIn("flowchart LR", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
