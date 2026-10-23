"""Leftovers whose names git would quote (accents, spaces, quotes), renames and untracked folders: `status --porcelain -z` gives the real
path, so Commit/Discard leftovers really clear the tree. Real git, temporary repo, no claude."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import close_loop  # noqa: E402
import loop_io  # noqa: E402
import run_loop  # noqa: E402

ACCENTED = "configuração.md"
SPACED = "my notes.txt"
QUOTED = 'say "hi".txt'


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True).stdout


class ParserCase(unittest.TestCase):
    def test_records_are_split_on_nul_and_renames_take_the_origin_record(self):
        text = " M a b.txt\0R  new name.txt\0old name.txt\0?? dir/\0A  x.md\0"
        rows = loop_io.parse_porcelain_z(text)
        self.assertEqual([row.path for row in rows], ["a b.txt", "new name.txt", "dir/", "x.md"])
        self.assertEqual([row.origin for row in rows], ["", "old name.txt", "", ""])
        self.assertEqual([row.status for row in rows], [" M", "R ", "??", "A "])
        self.assertTrue(rows[2].startswith("??"))

    def test_first_record_keeps_its_leading_space(self):
        self.assertEqual(loop_io.parse_porcelain_z(" M first.txt\0")[0].status, " M")

    def test_plain_text_rows_are_still_understood(self):
        self.assertEqual(run_loop.row_path('?? "configura\\303\\247\\303\\243o.md"'), ACCENTED)
        self.assertEqual(run_loop.row_path("R  old.txt -> new.txt"), "new.txt")
        self.assertEqual(run_loop.row_path("M  src/a.py"), "src/a.py")
        self.assertEqual(run_loop.row_path('?? "say \\"hi\\".txt"'), QUOTED)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="porcelain-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        sh(["git", "init", "-q", "-b", "main"], self.repo)
        for key, value in (("user.name", "t"), ("user.email", "t@t.t"), ("commit.gpgsign", "false")):
            sh(["git", "config", key, value], self.repo)
        self.write(ACCENTED, "base")
        self.write("renamed-source.txt", "content of the file that will be renamed")
        self.write("src/keep.txt", "base")
        self.write(".specs/project/ROADMAP.md", "## Stage 1\n")
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "commit", "-q", "-m", "base"], self.repo)
        self.opts = argparse.Namespace(leftovers="", primary_name="")

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def make_a_mess(self):
        self.write(ACCENTED, "changed")  # tracked, accented
        self.write(SPACED, "x")  # untracked, spaces
        self.write("src/deep folder/ação nova.txt", "x")  # untracked in a subfolder with accents and spaces
        self.write("src/other/file.txt", "x")
        sh(["git", "mv", "renamed-source.txt", "renamed target é.txt"], self.repo)  # staged rename
        self.write("staged new.txt", "x")
        sh(["git", "add", "staged new.txt"], self.repo)
        if os.name != "nt":
            self.write(QUOTED, "x")

    def porcelain(self):
        return sh(["git", "status", "--porcelain", "-z"], self.repo)

    def test_the_rows_hold_the_real_paths(self):
        self.make_a_mess()
        paths = {run_loop.row_path(row) for row in run_loop.dirty_outside_specs(self.repo, set())}
        for expected in (ACCENTED, SPACED, "src/deep folder/", "src/other/", "renamed target é.txt", "staged new.txt"):
            self.assertIn(expected, paths)

    def test_discard_leftovers_clears_the_tree(self):
        self.make_a_mess()
        self.opts.leftovers = "discard"
        self.assertEqual(run_loop.settle_dirty_worktrees(self.repo, {}, 4, self.opts), [])
        self.assertEqual(self.porcelain(), "")
        with open(os.path.join(self.repo, ACCENTED), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "base")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "renamed-source.txt")))  # the rename was undone
        self.assertFalse(os.path.exists(os.path.join(self.repo, "renamed target é.txt")))

    def test_commit_leftovers_clears_the_tree(self):
        self.make_a_mess()
        self.opts.leftovers = "commit"
        self.assertEqual(run_loop.settle_dirty_worktrees(self.repo, {}, 4, self.opts), [])
        self.assertEqual(self.porcelain(), "")
        tracked = sh(["git", "ls-files", "-z"], self.repo).split("\0")
        for expected in (SPACED, "src/deep folder/ação nova.txt", "renamed target é.txt", "staged new.txt"):
            self.assertIn(expected, tracked)
        self.assertNotIn("renamed-source.txt", tracked)

    def test_commit_with_glob_characters_in_the_name_takes_only_that_file(self):
        self.write("a[1].txt", "x")
        self.write("a1.txt", "y")
        rows = [row for row in run_loop.dirty_outside_specs(self.repo, set()) if run_loop.row_path(row) == "a[1].txt"]
        self.assertTrue(run_loop.commit_leftovers(self.repo, rows, "only the literal")[0])
        self.assertEqual(run_loop.row_path(run_loop.dirty_outside_specs(self.repo, set())[0]), "a1.txt")

    def test_ignored_files_inside_an_untracked_folder_are_expanded(self):
        self.write("cfg/local.env", "x")
        self.write("cfg/accentuação.txt", "x")
        rows = run_loop.dirty_outside_specs(self.repo, {"cfg/local.env"})
        self.assertEqual([run_loop.row_path(row) for row in rows], ["cfg/accentuação.txt"])

    def test_specs_changes_are_not_leftovers(self):
        self.write(".specs/project/STATE.md", "x")
        self.assertEqual(run_loop.dirty_outside_specs(self.repo, set()), [])

    def test_close_loop_reports_the_foreign_file_with_its_real_name(self):
        self.write(ACCENTED, "changed")
        self.assertEqual(close_loop.foreign_content_in_specs(self.repo, ()), ACCENTED)

    def test_close_loop_accepts_an_accented_saved_plan_file(self):
        self.write(ACCENTED, "changed")
        self.assertIsNone(close_loop.foreign_content_in_specs(self.repo, (ACCENTED,)))

    def test_close_loop_sees_a_status_on_the_very_first_line(self):
        self.write("src/keep.txt", "changed")  # ` M src/keep.txt`: the leading space must not be eaten
        self.assertEqual(close_loop.foreign_content_in_specs(self.repo, ()), "src/keep.txt")


if __name__ == "__main__":
    unittest.main()
