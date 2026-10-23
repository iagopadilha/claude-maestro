"""save_json (loop_io and close_loop) goes through a temporary file and os.replace: readers never see a half-written file."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import close_loop  # noqa: E402
import loop_io  # noqa: E402


class AtomicWriteCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="atomic-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = os.path.join(self.folder, "deep", "meta.json")

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_close_loop_shares_the_atomic_writer(self):
        self.assertIs(close_loop.save_json, loop_io.save_json)

    def test_writes_utf8_creates_the_folder_and_leaves_no_temporary_file(self):
        loop_io.save_json(self.path, {"name": "configuração", "n": 1})
        self.assertEqual(self.read(), {"name": "configuração", "n": 1})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["meta.json"])

    def test_a_failed_serialization_keeps_the_old_file(self):
        loop_io.save_json(self.path, {"ok": True})
        with self.assertRaises(TypeError):
            loop_io.save_json(self.path, {"bad": object()})
        self.assertEqual(self.read(), {"ok": True})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["meta.json"])

    def test_a_failed_replace_keeps_the_old_file_and_removes_the_temporary_one(self):
        loop_io.save_json(self.path, {"ok": True})
        with mock.patch.object(loop_io.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                loop_io.save_json(self.path, {"ok": False})
        self.assertEqual(self.read(), {"ok": True})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["meta.json"])

    def test_the_replace_is_retried_on_windows_permission_errors(self):
        loop_io.save_json(self.path, {"v": 1})
        real = os.replace
        calls = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("held open by a reader")
            real(source, target)

        with mock.patch.object(loop_io, "IS_WIN", True), mock.patch.object(loop_io.time, "sleep"), mock.patch.object(loop_io.os, "replace", flaky):
            loop_io.save_json(self.path, {"v": 2})
        self.assertEqual((len(calls), self.read()), (3, {"v": 2}))

    def test_a_permanent_permission_error_is_raised_after_the_attempts(self):
        loop_io.save_json(self.path, {"v": 1})
        with mock.patch.object(loop_io, "IS_WIN", True), mock.patch.object(loop_io.time, "sleep"), \
                mock.patch.object(loop_io.os, "replace", side_effect=PermissionError("locked")) as replace:
            with self.assertRaises(PermissionError):
                loop_io.save_json(self.path, {"v": 2})
        self.assertEqual(replace.call_count, loop_io.REPLACE_ATTEMPTS)
        self.assertEqual(self.read(), {"v": 1})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["meta.json"])

    def test_save_meta_still_keeps_the_app_owned_keys(self):
        loop_io.save_json(self.path, {"delivery": {"push": "yes"}, "max_cost_usd": 5})
        loop_io.save_meta(self.path, {"turns": 2, "delivery": {}})
        self.assertEqual(self.read(), {"turns": 2, "delivery": {"push": "yes"}, "max_cost_usd": 5})


if __name__ == "__main__":
    unittest.main()
