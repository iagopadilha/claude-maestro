"""T29 (domain/spec_import_scan: scan_folder limits, detect_items, warnings) and T30 (domain/import_coverage: parse and check, ranges)."""
import os
import shutil
import tempfile
import unittest

from harness_backend.domain.import_coverage import check_coverage, expand_item_range, parse_coverage
from harness_backend.domain.project_errors import InvalidRequestError
from harness_backend.domain.spec_import_scan import DEFAULT_LIMITS, build_warnings, detect_items, scan_folder

SPEC = """# Orig

## Goals

- ORIG-01 first goal
- ORIG-02..03 two more

## Out of Scope

| Item | Why |
| --- | --- |
| Billing | later |
| Mobile | later |

## Edge Cases

- empty file
- huge file

## Success Criteria

1. it works
2. it is fast

## Requirement Traceability

| Req | Task |
| --- | --- |
| CRIA-01 | T1 |

## Decisions

D-1 chose sqlite. D-2 chose rest.

## Open questions

None.
"""
TASKS = "# Tasks\n\n### T1: one\n\n### T2: two\n\n## T3: three\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = os.path.join(self.tmp, "orig")
        write(os.path.join(self.folder, "spec.md"), SPEC)
        write(os.path.join(self.folder, "tasks.md"), TASKS)

    def relative_paths(self, scan, status="read"):
        return [entry["relative_path"] for entry in scan["files"] if entry["status"] == status]

    def test_a_folder_with_a_subfolder_lists_relative_paths_sizes_and_hashes(self):
        write(os.path.join(self.folder, "extra", "notes.md"), "# Notes\n\nORIG-09 extra\n")
        scan = scan_folder(self.folder)
        self.assertIsNone(scan["refused"])
        self.assertEqual(self.relative_paths(scan), ["extra/notes.md", "spec.md", "tasks.md"])
        for entry in scan["files"]:
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertGreater(entry["size"], 0)
        self.assertEqual(scan["counts"]["files"], 3)

    def test_non_md_hidden_node_modules_and_deep_folders_are_skipped(self):
        write(os.path.join(self.folder, "image.png"), "x")
        write(os.path.join(self.folder, ".git", "a.md"), "# hidden")
        write(os.path.join(self.folder, "node_modules", "a.md"), "# dep")
        write(os.path.join(self.folder, "a", "b", "c", "d", "deep.md"), "# deep")
        write(os.path.join(self.folder, "a", "ok.md"), "# ok")
        scan = scan_folder(self.folder)
        self.assertEqual(self.relative_paths(scan), ["a/ok.md", "spec.md", "tasks.md"])
        reasons = {entry["relative_path"]: entry["reason"] for entry in scan["files"] if entry["status"] == "skipped"}
        self.assertEqual(reasons["image.png"], "not .md")
        self.assertIn("deeper than 3", reasons["a/b/c/d/"])
        self.assertNotIn(".git/a.md", reasons)

    def test_31_md_files_are_refused_with_the_counts_and_30_are_accepted(self):
        for index in range(28):
            write(os.path.join(self.folder, "f%02d.md" % index), "# F\n")
        self.assertIsNone(scan_folder(self.folder)["refused"], "spec + tasks + 28 = 30")
        write(os.path.join(self.folder, "one-more.md"), "# F\n")
        refused = scan_folder(self.folder)["refused"]
        self.assertIn("the folder has 31 .md files: the limit is 30", refused["reason"])
        self.assertEqual(refused["counts"]["files"], 31)

    def test_a_file_over_400_kb_an_empty_file_and_a_missing_spec_are_refused(self):
        write(os.path.join(self.folder, "big.md"), "x" * (DEFAULT_LIMITS["file_bytes"] + 1))
        self.assertIn("larger than 400 KB", scan_folder(self.folder)["refused"]["reason"])
        os.remove(os.path.join(self.folder, "big.md"))
        write(os.path.join(self.folder, "empty.md"), "  \n")
        self.assertIn("is empty", scan_folder(self.folder)["refused"]["reason"])
        os.remove(os.path.join(self.folder, "empty.md"))
        os.remove(os.path.join(self.folder, "spec.md"))
        self.assertIn("no spec.md", scan_folder(self.folder)["refused"]["reason"])

    def test_the_total_over_2_mb_is_refused(self):
        for index in range(6):
            write(os.path.join(self.folder, "part%d.md" % index), "# P\n" + "y" * (390 * 1024))
        self.assertIn("the limit is 2048 KB", scan_folder(self.folder)["refused"]["reason"])

    def test_a_file_or_a_missing_folder_is_an_invalid_request(self):
        with self.assertRaises(InvalidRequestError):
            scan_folder(os.path.join(self.folder, "spec.md"))
        with self.assertRaises(InvalidRequestError):
            scan_folder(os.path.join(self.tmp, "nowhere"))

    def test_detect_items_finds_ids_ranges_sections_and_counts(self):
        files = [entry for entry in scan_folder(self.folder)["files"]]
        items = detect_items(files)
        for expected in ("ORIG-01", "ORIG-02", "ORIG-03", "CRIA-01"):
            self.assertIn(expected, items["requirements"])
        self.assertEqual(items["decisions"], ["D-1", "D-2"])
        self.assertEqual(items["tasks"], ["T1", "T2", "T3"])
        self.assertTrue(all(items["sections"][name] for name in ("Goals", "Out of Scope", "Edge Cases", "Success Criteria", "Requirement Traceability")))
        self.assertEqual((items["out_of_scope_rows"], items["edge_cases"], items["success_criteria"]), (2, 2, 2))

    def test_a_free_form_spec_warns_no_ids_and_names_missing_sections(self):
        write(os.path.join(self.folder, "spec.md"), "# Just prose\n\nBuild a thing that exports data.\n")
        write(os.path.join(self.folder, "tasks.md"), "# nothing\n")
        scan = scan_folder(self.folder)
        items = detect_items(scan["files"])
        warnings = build_warnings(scan, items)
        self.assertIn("no IDs: coverage by section only", warnings)
        self.assertIn("missing section: Goals", warnings)

    def test_open_questions_and_conformance_mismatch_warn(self):
        write(os.path.join(self.folder, "spec.md"), SPEC.replace("None.", "Which database?") + "\nConformance: LEGACY\n")
        scan = scan_folder(self.folder)
        warnings = build_warnings(scan, detect_items(scan["files"]), "STANDARD")
        self.assertTrue(any("open questions" in item for item in warnings), warnings)
        self.assertTrue(any("Conformance LEGACY but the project is STANDARD" in item for item in warnings), warnings)
        self.assertFalse(any("no IDs" in item for item in warnings))


TABLE = """## orig

| Item | Kind | Destination |
| --- | --- | --- |
| ORIG-01..03 | requirement | ROADMAP:[orig] reqs |
| D-1 | decision | dropped: superseded |
| T1 | task |  |
"""


class CoverageTests(unittest.TestCase):
    ITEMS = {"requirements": ["ORIG-01", "ORIG-02", "ORIG-03", "ORIG-04"], "decisions": ["D-1"], "tasks": ["T1"], "sections": {}}

    def test_ranges_expand_and_keep_zero_padding(self):
        self.assertEqual(expand_item_range("ORIG-01..03"), ["ORIG-01", "ORIG-02", "ORIG-03"])
        self.assertEqual(expand_item_range("R-9..11"), ["R-9", "R-10", "R-11"])
        self.assertEqual(expand_item_range("ORIG-05..02"), ["ORIG-05..02"])
        self.assertEqual(expand_item_range("plain"), ["plain"])

    def test_parse_groups_by_slug_and_ignores_header_and_separator(self):
        coverage = parse_coverage(TABLE + "\n## second\n\n| Item | Kind | Destination |\n| --- | --- | --- |\n| ORIG-04 | requirement | RULES.md |\n")
        self.assertEqual(sorted(coverage), ["orig", "second"])
        self.assertEqual(sorted(coverage["orig"]), ["D-1", "ORIG-01", "ORIG-02", "ORIG-03", "T1"])
        self.assertEqual(coverage["second"], {"ORIG-04": "RULES.md"})

    def test_a_table_covering_every_id_has_nothing_missing(self):
        text = TABLE.replace("| T1 | task |  |", "| T1 | task | ROADMAP:[orig] |") + "| ORIG-04 | requirement | RULES.md |\n"
        result = check_coverage(self.ITEMS, parse_coverage(text))
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["dropped"], [{"item": "D-1", "reason": "superseded"}])
        self.assertEqual(len(result["covered"]), 5)

    def test_removing_a_row_puts_that_id_in_missing(self):
        text = TABLE.replace("| T1 | task |  |\n", "") + "| ORIG-04 | requirement | RULES.md |\n"
        self.assertEqual(check_coverage(self.ITEMS, parse_coverage(text))["missing"], ["T1"])

    def test_a_range_counts_as_three_ids(self):
        result = check_coverage({"requirements": ["ORIG-01", "ORIG-02", "ORIG-03"]}, parse_coverage(TABLE))
        self.assertEqual((result["covered"], result["missing"]), (["ORIG-01", "ORIG-02", "ORIG-03"], []))

    def test_an_empty_destination_is_missing(self):
        self.assertIn("T1", check_coverage(self.ITEMS, parse_coverage(TABLE))["missing"])

    def test_without_ids_the_sections_present_are_the_items(self):
        items = {"requirements": [], "decisions": [], "tasks": [], "sections": {"Goals": True, "Edge Cases": False}}
        self.assertEqual(check_coverage(items, parse_coverage("| Goals | goal | PROJECT.md |"))["covered"], ["Goals"])
        self.assertEqual(check_coverage(items, {})["missing"], ["Goals"])


if __name__ == "__main__":
    unittest.main()
