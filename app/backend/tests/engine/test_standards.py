#!/usr/bin/env python3
"""Standards slots (engine/standards.py): parse, render, write (append, replace, remove, other lines and newline style kept), skill_name,
project_bindings (monorepo: subprojects only, rule R-03) and the CLI (`harness.py standards`). Temp dirs only."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import standards  # noqa: E402

FULL = "# Project\n\nSome text.\n\n## Standards\ngit: acme-git\nbackend: acme:acme-backend\nfrontend: acme-front\n\n## Other\nkeep me\n"


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="standards-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.claude = os.path.join(self.root, "CLAUDE.md")

    def put(self, text):
        write_bytes(self.claude, text.encode("utf-8"))

    def text(self):
        return read_bytes(self.claude).decode("utf-8")


class TestParse(unittest.TestCase):
    def test_reads_the_bound_slots(self):
        parsed = standards.parse(FULL)
        self.assertEqual(parsed["bindings"], {"git": "acme-git", "backend": "acme:acme-backend", "frontend": "acme-front"})
        self.assertEqual(parsed["notes"], [])

    def test_missing_section_is_empty_without_notes(self):
        self.assertEqual(standards.parse("# Title\nbackend: x\n"), {"bindings": {}, "notes": []})
        self.assertEqual(standards.parse(""), {"bindings": {}, "notes": []})
        self.assertEqual(standards.parse(None), {"bindings": {}, "notes": []})

    def test_empty_value_is_ignored_with_a_note(self):
        parsed = standards.parse("## Standards\ngit:\nbackend: ok\n")
        self.assertEqual(parsed["bindings"], {"backend": "ok"})
        self.assertEqual(len(parsed["notes"]), 1)
        self.assertIn("git", parsed["notes"][0])

    def test_unknown_slot_is_ignored_with_a_note(self):
        parsed = standards.parse("## Standards\ndatabase: acme-db\nfrontend: acme-front\n")
        self.assertEqual(parsed["bindings"], {"frontend": "acme-front"})
        self.assertIn("database", parsed["notes"][0])

    def test_qualified_name_is_kept_as_written(self):
        self.assertEqual(standards.parse("## Standards\nobservability: acme:acme-grafana\n")["bindings"], {"observability": "acme:acme-grafana"})

    def test_a_value_that_is_not_a_skill_name_is_a_note(self):
        parsed = standards.parse("## Standards\nbackend: two words\napp-map: a/b\n")
        self.assertEqual(parsed["bindings"], {})
        self.assertEqual(len(parsed["notes"]), 2)

    def test_a_line_without_a_colon_is_a_note_and_blank_lines_are_fine(self):
        parsed = standards.parse("## Standards\n\nrandom words\ngit: g\n")
        self.assertEqual(parsed["bindings"], {"git": "g"})
        self.assertEqual(len(parsed["notes"]), 1)

    def test_the_section_ends_at_the_next_heading(self):
        parsed = standards.parse("## Standards\ngit: g\n### Sub\nbackend: b\n")
        self.assertEqual(parsed["bindings"], {"git": "g"})

    def test_slot_names_are_case_insensitive_and_crlf_is_read(self):
        parsed = standards.parse("## Standards\r\nGit: g\r\nBackend:   b  \r\n")
        self.assertEqual(parsed["bindings"], {"git": "g", "backend": "b"})

    def test_a_heading_inside_a_code_fence_is_not_the_section(self):
        self.assertEqual(standards.parse("```markdown\n## Standards\ngit: g\n```\n")["bindings"], {})

    def test_read_of_a_missing_file_or_folder(self):
        self.assertEqual(standards.read(os.path.join(tempfile.gettempdir(), "no-such-folder-standards")), {"bindings": {}, "notes": []})


class TestRender(unittest.TestCase):
    def test_slots_come_in_the_declared_order(self):
        text = standards.render({"observability": "o", "git": "g", "frontend": "f"})
        self.assertEqual(text, "## Standards\ngit: g\nfrontend: f\nobservability: o\n")

    def test_empty_gives_nothing(self):
        self.assertEqual(standards.render({}), "")
        self.assertEqual(standards.render({"git": "  "}), "")

    def test_crlf_ending(self):
        self.assertEqual(standards.render({"git": "g"}, "\r\n"), "## Standards\r\ngit: g\r\n")


class TestSkillName(unittest.TestCase):
    def test_same_rule_as_the_guard(self):
        self.assertEqual(standards.skill_name("Acme:Acme-Backend "), "acme-backend")
        self.assertEqual(standards.skill_name("plain"), "plain")
        self.assertEqual(standards.skill_name("/Plugin:/Skill"), "skill")
        self.assertEqual(standards.skill_name(None), "")


class TestWrite(TempCase):
    def test_creates_the_file_when_missing(self):
        result = standards.write(self.root, {"git": "acme-git"})
        self.assertEqual(self.text(), "## Standards\ngit: acme-git\n")
        self.assertEqual(result["bindings"], {"git": "acme-git"})

    def test_appends_after_the_existing_content_with_one_blank_line(self):
        self.put("# Project\n\nrules here\n")
        standards.write(self.root, {"backend": "acme-backend"})
        self.assertEqual(self.text(), "# Project\n\nrules here\n\n## Standards\nbackend: acme-backend\n")

    def test_appends_when_the_file_has_no_final_newline(self):
        self.put("# Project")
        standards.write(self.root, {"git": "g"})
        self.assertEqual(self.text(), "# Project\n\n## Standards\ngit: g\n")

    def test_replaces_the_section_and_keeps_every_other_line(self):
        self.put(FULL)
        standards.write(self.root, {"git": "new-git", "observability": "obs"})
        self.assertEqual(self.text(), "# Project\n\nSome text.\n\n## Standards\ngit: new-git\nobservability: obs\n\n## Other\nkeep me\n")

    def test_remove_with_an_empty_mapping_and_the_rest_is_intact(self):
        self.put(FULL)
        result = standards.write(self.root, {})
        self.assertEqual(self.text(), "# Project\n\nSome text.\n\n## Other\nkeep me\n")
        self.assertEqual(result["bindings"], {})

    def test_append_then_remove_gives_back_the_original(self):
        original = "# Project\n\nrules here\n"
        self.put(original)
        standards.write(self.root, {"git": "g"})
        standards.write(self.root, {})
        self.assertEqual(self.text(), original)

    def test_removing_from_a_file_without_a_section_changes_nothing_and_creates_nothing(self):
        standards.write(self.root, {})
        self.assertFalse(os.path.exists(self.claude))
        self.put("# Project\n")
        before = read_bytes(self.claude)
        standards.write(self.root, {})
        self.assertEqual(read_bytes(self.claude), before)

    def test_writing_is_idempotent(self):
        self.put(FULL)
        standards.write(self.root, {"git": "acme-git", "backend": "acme:acme-backend", "frontend": "acme-front"})
        first = read_bytes(self.claude)
        standards.write(self.root, {"git": "acme-git", "backend": "acme:acme-backend", "frontend": "acme-front"})
        self.assertEqual(read_bytes(self.claude), first)
        self.assertEqual(first.decode("utf-8"), FULL)

    def test_crlf_files_keep_crlf_everywhere(self):
        self.put(FULL.replace("\n", "\r\n"))
        standards.write(self.root, {"git": "g"})
        data = read_bytes(self.claude)
        self.assertEqual(data.decode("utf-8"), "# Project\r\n\r\nSome text.\r\n\r\n## Standards\r\ngit: g\r\n\r\n## Other\r\nkeep me\r\n")
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_appending_to_a_crlf_file_uses_crlf(self):
        self.put("# Project\r\nline\r\n")
        standards.write(self.root, {"git": "g"})
        self.assertEqual(self.text(), "# Project\r\nline\r\n\r\n## Standards\r\ngit: g\r\n")

    def test_a_bom_is_kept(self):
        write_bytes(self.claude, b"\xef\xbb\xbf# Project\nline\n")
        standards.write(self.root, {"git": "g"})
        self.assertTrue(read_bytes(self.claude).startswith(b"\xef\xbb\xbf# Project"))
        self.assertEqual(standards.read(self.root)["bindings"], {"git": "g"})

    def test_a_section_at_the_end_of_the_file_is_replaced(self):
        self.put("# Project\n\n## Standards\ngit: old\n")
        standards.write(self.root, {"git": "new"})
        self.assertEqual(self.text(), "# Project\n\n## Standards\ngit: new\n")

    def test_empty_values_clear_their_slot(self):
        self.put(FULL)
        result = standards.write(self.root, {"git": "", "backend": "b"})
        self.assertEqual(result["bindings"], {"backend": "b"})

    def test_unknown_slot_and_invalid_value_are_refused_naming_the_slot_and_nothing_is_written(self):
        self.put(FULL)
        for bindings, needle in (({"database": "x"}, "database"), ({"backend": "two words"}, "backend"), ({"git": "a/b"}, "git")):
            with self.assertRaises(ValueError) as caught:
                standards.write(self.root, bindings)
            self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.text(), FULL)

    def test_no_temp_file_is_left_behind(self):
        standards.write(self.root, {"git": "g"})
        self.assertEqual(os.listdir(self.root), ["CLAUDE.md"])


class TestProjectBindings(TempCase):
    def bind(self, folder, text):
        write_bytes(os.path.join(folder, "CLAUDE.md"), ("## Standards\n%s\n" % text).encode("utf-8"))

    def subprojects(self, projects, repo_name=None):
        name = repo_name or os.path.basename(self.root)
        write_bytes(os.path.join(self.root, ".specs", "project", "SUBPROJECTS.json"),
                    json.dumps({"repos": {name: {"monorepo": True, "confirmed": True, "projects": projects}}}).encode("utf-8"))

    def test_a_single_project_is_the_root(self):
        self.bind(self.root, "git: g")
        self.assertEqual(standards.project_bindings(self.root), [{"folder": ".", "bindings": {"git": "g"}}])

    def test_no_root_gives_nothing(self):
        self.assertEqual(standards.project_bindings(""), [])
        self.assertEqual(standards.project_bindings(None), [])

    def test_a_monorepo_reads_each_subproject_and_skips_the_root(self):
        self.bind(self.root, "git: root-git")
        self.bind(os.path.join(self.root, "api"), "backend: api-backend")
        self.bind(os.path.join(self.root, "apps", "web"), "frontend: web-front")
        self.subprojects([{"path": "api"}, {"path": "apps/web"}, {"path": "seed", "skip": True}])
        self.assertEqual(standards.project_bindings(self.root), [{"folder": "api", "bindings": {"backend": "api-backend"}},
                                                                 {"folder": "apps/web", "bindings": {"frontend": "web-front"}}])

    def test_a_subproject_without_a_section_has_empty_bindings(self):
        self.subprojects([{"path": "api"}])
        os.makedirs(os.path.join(self.root, "api"))
        self.assertEqual(standards.project_bindings(self.root), [{"folder": "api", "bindings": {}}])


class TestCommandLine(TempCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = standards.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_bindings_as_json(self):
        self.put(FULL)
        code, out, _ = self.run_main(self.root, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["bindings"]["frontend"], "acme-front")
        self.assertEqual(payload["slots"], list(standards.SLOTS))

    def test_set_and_clear_edit_only_what_was_named(self):
        self.put(FULL)
        code, out, _ = self.run_main(self.root, "--set", "git=other-git", "observability=acme:obs", "--clear", "frontend", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["bindings"], {"git": "other-git", "backend": "acme:acme-backend", "observability": "acme:obs"})
        self.assertEqual(standards.read(self.root)["bindings"]["backend"], "acme:acme-backend")

    def test_text_output_lists_every_slot(self):
        self.put(FULL)
        code, out, _ = self.run_main(self.root)
        self.assertEqual(code, 0)
        for slot in standards.SLOTS:
            self.assertIn(slot, out)

    def test_bad_input_is_a_usage_error_and_changes_nothing(self):
        self.put(FULL)
        for argv in (("--set", "database=x"), ("--set", "backend"), ("--set", "backend=two words"), ("--clear", "nope")):
            code, _, err = self.run_main(self.root, *argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.strip(), argv)
        self.assertEqual(self.text(), FULL)
        self.assertEqual(self.run_main(os.path.join(self.root, "missing"))[0], 2)

    def test_a_monorepo_root_refuses_edits_and_a_subproject_takes_them(self):
        write_bytes(os.path.join(self.root, ".specs", "project", "SUBPROJECTS.json"), json.dumps(
            {"repos": {os.path.basename(self.root): {"monorepo": True, "confirmed": True, "projects": [{"path": "api"}]}}}).encode("utf-8"))
        os.makedirs(os.path.join(self.root, "api"))
        code, _, err = self.run_main(self.root, "--set", "git=g")
        self.assertEqual(code, 2)
        self.assertIn("--subproject", err)
        self.assertFalse(os.path.exists(self.claude), "the root CLAUDE.md is never written")
        code, out, _ = self.run_main(self.root, "--subproject", "api", "--set", "backend=api-backend", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["bindings"], {"backend": "api-backend"})
        self.assertEqual(standards.read(os.path.join(self.root, "api"))["bindings"], {"backend": "api-backend"})

    def test_a_missing_subproject_folder_is_a_usage_error(self):
        self.assertEqual(self.run_main(self.root, "--subproject", "nope")[0], 2)


if __name__ == "__main__":
    unittest.main()
