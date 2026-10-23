"""LOOP.lock: created atomically (O_EXCL), stale only when its owner is gone or the PID now belongs to another process (start time differs);
one helper (`lock_holder_alive`) answers for the engine, close_loop and rewind."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import close_loop  # noqa: E402
import loop_io  # noqa: E402
import loop_state  # noqa: E402
import run_loop  # noqa: E402
import worktree_state  # noqa: E402


def dead_pid():
    """PID of a process that already exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class LockCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="lock-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = loop_state.loop_path(self.work, "LOOP.lock")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def write_lock(self, text, started=None, pid=None):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        if started is not None:
            with open(self.path + loop_io.LOCK_START_SUFFIX, "w", encoding="utf-8") as handle:
                json.dump({"pid": pid if pid is not None else int(text), "started": started}, handle)

    def test_acquire_writes_the_plain_pid_and_the_start_time_beside_it(self):
        self.assertEqual(worktree_state.acquire_lock(self.work), self.path)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(int(handle.read().strip()), os.getpid())  # the panel readers still parse it with int()
        started = loop_io.process_start_time(os.getpid())
        if started is not None:
            self.assertEqual(loop_io.read_lock_holder(self.path), (os.getpid(), started, False))
        self.assertTrue(loop_io.lock_holder_alive(self.path))

    def test_run_loop_still_exports_the_lock_functions(self):
        self.assertIs(run_loop.acquire_lock, worktree_state.acquire_lock)
        self.assertIs(run_loop.release_lock, worktree_state.release_lock)

    def test_a_live_lock_stops_the_second_loop_and_is_left_alone(self):
        self.write_lock(str(os.getpid()))
        with self.assertRaises(SystemExit) as stop:
            worktree_state.acquire_lock(self.work)
        self.assertIn("already running", str(stop.exception.code))
        self.assertEqual(loop_io.read_lock_holder(self.path)[0], os.getpid())

    def test_a_dead_pid_is_stale_and_replaced_by_this_process(self):
        self.write_lock(str(dead_pid()))
        worktree_state.acquire_lock(self.work)
        self.assertEqual(loop_io.read_lock_holder(self.path)[0], os.getpid())
        self.assertEqual([name for name in os.listdir(os.path.dirname(self.path)) if "stale" in name], [])

    def test_a_reused_pid_with_another_start_time_is_stale(self):
        now = loop_io.process_start_time(os.getpid())
        if now is None:
            self.skipTest("start time of a process is not available on this system")
        self.write_lock(str(os.getpid()), started=now - 12345)  # "the loop" that wrote it started earlier than the process that has the PID now
        self.assertFalse(loop_io.lock_holder_alive(self.path))
        worktree_state.acquire_lock(self.work)
        self.assertEqual(loop_io.read_lock_holder(self.path)[:2], (os.getpid(), now))

    def test_the_panel_readers_see_no_pid_when_it_was_reused(self):
        now = loop_io.process_start_time(os.getpid())
        if now is None:
            self.skipTest("start time of a process is not available on this system")
        self.write_lock(str(os.getpid()), started=now - 12345)
        self.assertEqual(loop_io.lock_owner_pid(self.path), 0)
        self.write_lock(str(os.getpid()), started=now)
        self.assertEqual(loop_io.lock_owner_pid(self.path), os.getpid())
        self.write_lock("4242")  # old lock without a start time: the pid as written
        os.remove(self.path + loop_io.LOCK_START_SUFFIX)
        self.assertEqual(loop_io.lock_owner_pid(self.path), 4242)

    def test_the_matching_start_time_keeps_the_lock_alive(self):
        now = loop_io.process_start_time(os.getpid())
        if now is None:
            self.skipTest("start time of a process is not available on this system")
        self.write_lock(str(os.getpid()), started=now)
        self.assertTrue(loop_io.lock_holder_alive(self.path))

    def test_a_sidecar_about_another_pid_is_ignored(self):
        self.write_lock(str(os.getpid()), started=1, pid=os.getpid() + 1)
        self.assertTrue(loop_io.lock_holder_alive(self.path))

    def test_old_formats_keep_working(self):
        self.write_lock(str(os.getpid()))  # plain PID, no start time
        self.assertTrue(loop_io.lock_holder_alive(self.path))
        self.write_lock(str(dead_pid()))
        self.assertFalse(loop_io.lock_holder_alive(self.path))
        self.write_lock("not a pid")
        self.assertFalse(loop_io.lock_holder_alive(self.path))
        self.assertFalse(loop_io.lock_holder_alive(self.path + ".missing"))

    def test_an_empty_lock_is_a_loop_still_writing_it_until_it_gets_old(self):
        self.write_lock("")
        self.assertTrue(loop_io.lock_holder_alive(self.path))
        old = time.time() - loop_io.LOCK_FRESH_SECONDS - 10
        os.utime(self.path, (old, old))
        self.assertFalse(loop_io.lock_holder_alive(self.path))

    def test_release_removes_only_the_lock_of_this_process(self):
        path = worktree_state.acquire_lock(self.work)
        worktree_state.release_lock(path)
        self.assertEqual(os.listdir(os.path.dirname(path)), [])
        self.write_lock(str(os.getpid() + 1))
        worktree_state.release_lock(self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_the_stale_lock_goes_back_when_it_was_replaced_by_a_live_one_in_between(self):
        self.write_lock(str(os.getpid()))
        worktree_state.remove_stale_lock(self.path, dead_pid())  # judged stale with another PID, but the file now names a live one
        self.assertEqual(loop_io.read_lock_holder(self.path)[0], os.getpid())

    def test_racing_processes_get_exactly_one_lock(self):
        winners, losers = [], []
        gate = threading.Barrier(8)

        def contender():
            gate.wait()
            try:
                worktree_state.create_lock_file(self.path)
                winners.append(1)
            except FileExistsError:
                losers.append(1)

        threads = [threading.Thread(target=contender) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((len(winners), len(losers)), (1, 7))

    def test_close_loop_and_rewind_use_the_same_helper(self):
        self.assertTrue(close_loop.loop_is_running(self.work) is False)
        self.write_lock(str(os.getpid()))
        self.assertTrue(close_loop.loop_is_running(self.work))
        now = loop_io.process_start_time(os.getpid())
        if now is not None:
            self.write_lock(str(os.getpid()), started=now - 1)
            self.assertFalse(close_loop.loop_is_running(self.work))
        with mock.patch.object(worktree_state, "lock_holder_alive", return_value=False) as helper:
            self.write_lock(str(os.getpid()))
            with mock.patch.object(worktree_state, "worktree_of", return_value=self.work), \
                    mock.patch.object(worktree_state.loop_state, "migrate_layout"), \
                    mock.patch.object(worktree_state, "load_json", return_value={"branch": "loop/x"}), \
                    mock.patch.object(worktree_state.loop_state, "load_snapshot", return_value=None):
                with self.assertRaises(SystemExit) as stop:
                    worktree_state.rewind("repo", mock.Mock(rewind=1))
            self.assertIn("no snapshot", str(stop.exception.code))  # the rewind went past the running check because the helper said "not alive"
            helper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
