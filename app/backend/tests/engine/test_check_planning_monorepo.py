"""`run_loop.py <repo> --check --planning` on a confirmed monorepo and on a single project (C3). Real script, scratch git repos, fake claude."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
from test_check_planning import PlanningCheckCase, write  # noqa: E402
from test_loop import sh  # noqa: E402

UNCOMMITTED_SENTENCE = ("These files are not committed. The loop worktree is created when the loop starts, from the last commit, "
                        "so the agents will not see them. Commit them first.")


def roadmap_of(*lines):
    return "# Roadmap\n## Stage 1 - Base\n" + "".join(line + "\n" for line in lines)


class MonorepoCheckCase(PlanningCheckCase):
    def make_monorepo(self, roadmap, projects=("backend", "frontend"), conformance=("backend",), mapped=("backend",), skipped=(), commit=True):
        project = self.make_planning(roadmap)
        self.servers("grafana")
        entries = []
        for name in projects:
            entries.append({"path": name, "name": name, "stack": "x", "skill": "-", "skip": name in skipped, "has_code": True})
            write(os.path.join(project.repo, name, "src", "main.txt"), "code\n")
            if name in conformance:
                write(os.path.join(project.repo, name, "CLAUDE.md"), "Conformance: STANDARD\n")
            if name in mapped:
                write(os.path.join(project.repo, name, "docs", "codebase", "STACK.md"), "map\n")
        entry = {"monorepo": True, "confirmed": True, "projects": entries}
        write(os.path.join(project.repo, ".specs", "project", "SUBPROJECTS.json"), json.dumps({"version": 1, "repos": {"proj": entry}}))
        if commit:
            sh(["git", "add", "-A"], project.repo)
            sh(["git", "commit", "-q", "-m", "monorepo"], project.repo)
        return project


class TestMonorepoCheck(MonorepoCheckCase):
    def test_frontend_without_conformance_is_a_problem_naming_it(self):
        project = self.make_monorepo(roadmap_of("- [ui] status=pending paths=frontend/src"), mapped=("backend", "frontend"))
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("PROBLEM: subproject `frontend` is touched by the roadmap but has no `Conformance", result.stdout)

    def test_backend_only_feature_passes_and_root_conformance_is_not_asked(self):
        project = self.make_monorepo(roadmap_of("- [api] status=pending paths=backend/src"))
        os.remove(os.path.join(project.repo, "CLAUDE.md")) if os.path.exists(os.path.join(project.repo, "CLAUDE.md")) else None
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("[check] READY", result.stdout)
        self.assertIn("WARNING: subproject `frontend` is not touched by any feature yet", result.stdout)

    def test_root_only_paths_warn_and_stay_ready(self):
        project = self.make_monorepo(roadmap_of("- [ci] status=pending paths=scripts"))
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("paths outside any subproject (scripts): the agents use the root rules only", result.stdout)

    def test_skipped_subproject_warns(self):
        project = self.make_monorepo(roadmap_of("- [t] status=pending paths=tools/x"), projects=("backend", "tools"), skipped=("tools",))
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("subproject `tools` skipped: no map, no Conformance for its agents", result.stdout)

    def test_uncommitted_context_warns_with_the_exact_text_and_stays_ready(self):
        project = self.make_monorepo(roadmap_of("- [api] status=pending paths=backend/src"), commit=False)
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("[check] READY", result.stdout)
        self.assertIn("WARNING: uncommitted changes in proj: ", result.stdout)
        self.assertIn("setup files not committed: they will be carried into the worktree (or commit them from Workspace)", result.stdout)
        self.assertIn("backend/CLAUDE.md", result.stdout)
        sh(["git", "add", "-A"], project.repo)
        sh(["git", "commit", "-q", "-m", "all"], project.repo)
        self.assertNotIn("setup files not committed", project.run("--check", "--planning").stdout)

    def test_giant_file_warns_and_is_cleared_by_refactor_or_note(self):
        project = self.make_monorepo(roadmap_of("- [api] status=pending paths=backend/src/Big.cs"))
        write(os.path.join(project.repo, ".specs", "project", "loop", "internal", "inventory", "backend.json"),
              json.dumps({"giant": [{"path": "backend/src/Big.cs", "lines": 2500, "bytes": 9}]}))
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("giant file(s) touched without a refactor: backend/src/Big.cs (2500 lines); features: api", result.stdout)
        write(os.path.join(project.repo, ".specs", "project", "ROADMAP.md"), roadmap_of(
            "- [refactor-big] status=pending paths=backend/src/Big.cs", "- [api] status=pending paths=backend/src deps=refactor-big"))
        self.assertNotIn("giant file(s)", project.run("--check", "--planning").stdout)
        write(os.path.join(project.repo, ".specs", "project", "ROADMAP.md"), roadmap_of("- [api] status=pending paths=backend/src note=giant-accepted"))
        self.assertNotIn("giant file(s)", project.run("--check", "--planning").stdout)

    def test_two_subprojects_without_system_map_warn(self):
        project = self.make_monorepo(roadmap_of("- [api] status=pending paths=backend/src", "- [ui] status=pending paths=frontend/src"),
                                     conformance=("backend", "frontend"), mapped=("backend", "frontend"))
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("no system map: the agents have no description of how the subprojects talk", result.stdout)
        write(os.path.join(project.repo, "docs", "codebase", "SYSTEM.md"), "system\n")
        self.assertNotIn("no system map", project.run("--check", "--planning").stdout)


class TestSingleProjectUnchanged(MonorepoCheckCase):
    def test_single_project_keeps_the_root_conformance_rule(self):
        project = self.make_planning(roadmap_of("- [api] status=pending paths=src"))
        self.servers("grafana")
        os.remove(os.path.join(project.repo, "CLAUDE.md")) if os.path.exists(os.path.join(project.repo, "CLAUDE.md")) else None
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("CLAUDE.md has no `Conformance: STANDARD|LEGACY` line", result.stdout)
        self.assertNotIn("subproject", result.stdout)
        self.assertNotIn("no system map", result.stdout)

    def test_single_project_warns_about_uncommitted_root_context(self):
        project = self.make_planning(roadmap_of("- [api] status=pending paths=src"))
        self.servers("grafana")
        write(os.path.join(project.repo, "docs", "codebase", "STACK.md"), "map\n")
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("setup files not committed: they will be carried into the worktree (or commit them from Workspace)", result.stdout)
        self.assertIn("docs/codebase/STACK.md", result.stdout)


if __name__ == "__main__":
    unittest.main()
