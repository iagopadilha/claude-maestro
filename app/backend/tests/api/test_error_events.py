"""Turn timeline, launch log parser and the durable error event store behind the error center."""
import os
import tempfile
import unittest

from harness_backend.repositories.error_event_repository import ErrorEventRepository
from harness_backend.services.launch_log_parser import collect_turn_starts, parse_launch_log
from harness_backend.services.turn_timeline import TurnTimeline


class TestTurnTimeline(unittest.TestCase):
    def test_window_start_prefers_the_launch_log_then_the_duration_then_the_previous_end(self):
        timeline = TurnTimeline.build({1: 100, 2: 200, 3: 300}, starts={1: 90}, durations={2: 50})
        self.assertEqual((timeline.turn_at(95), timeline.turn_at(160), timeline.turn_at(250)), (1, 2, 3))
        self.assertIsNone(timeline.turn_at(60))   # before the first window
        self.assertIsNone(timeline.turn_at(120))  # turn 2 started at 150 by its duration, turn 1 ended at 100: the gap is nobody's
        self.assertIsNone(timeline.turn_at(None))

    def test_a_turn_without_any_start_is_never_guessed(self):
        timeline = TurnTimeline.build({1: 100})
        self.assertIsNone(timeline.turn_at(50))
        self.assertIsNone(timeline.turn_at(100))

    def test_the_turn_in_progress_has_an_open_end(self):
        timeline = TurnTimeline.build({1: 100}, starts={1: 10}, running_turn=2)
        self.assertEqual((timeline.turn_at(500), timeline.turn_at(50)), (2, 1))
        self.assertIsNone(TurnTimeline.build({1: 100}, starts={1: 10}).turn_at(500))

    def test_overlap_goes_to_the_later_turn(self):
        timeline = TurnTimeline.build({1: 100, 2: 200}, starts={1: 0, 2: 100})
        self.assertEqual(timeline.turn_at(100), 2)


class TestLaunchLogParser(unittest.TestCase):
    def test_turn_starts_and_the_turn_of_each_line(self):
        launches = parse_launch_log("[loop-launch] 2026-10-19T23:59:50 loop_start\nwarm\n[loop] turn 1 | 23:59:58\nx\n[loop] turn 2 | 00:01:10\ny\n"
                                    "[loop-launch] 2026-10-20T08:00:00 resume\n[loop] turn 2 | 08:00:05\n")
        self.assertEqual([launch["marker"] for launch in launches], ["2026-10-19T23:59:50 loop_start", "2026-10-20T08:00:00 resume"])
        self.assertEqual([(line["text"], line["turn"]) for line in launches[0]["lines"]][:2], [("warm", None), ("[loop] turn 1 | 23:59:58", 1)])
        self.assertEqual(launches[0]["first_turn"], 1)
        first = launches[0]["turn_starts"]
        self.assertGreater(first[2], first[1])  # midnight crossed: turn 2 is the next day
        self.assertEqual(round(first[2] - first[1]), 72)
        self.assertEqual(collect_turn_starts(launches)[2], launches[1]["turn_starts"][2])  # the later launch wins

    def test_a_log_without_marker_is_one_launch_and_an_empty_log_none(self):
        self.assertEqual(len(parse_launch_log("just text\n")), 1)
        self.assertEqual(parse_launch_log(""), [])


class TestErrorEventRepository(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="errevents-")
        self.repository = ErrorEventRepository()

    def test_missing_file_is_empty_and_a_later_record_only_fills_fields(self):
        self.assertEqual(self.repository.read_events(self.work), [])
        self.assertTrue(self.repository.append_events(self.work, [{"id": "a", "kind": "guard", "turn": None, "rule": "x"}]))
        self.assertTrue(self.repository.append_events(self.work, [{"id": "a", "turn": 4}, {"id": "a", "turn": None}, {"id": "b", "kind": "guard"}]))
        events = {event["id"]: event for event in self.repository.read_events(self.work)}
        self.assertEqual((events["a"]["turn"], events["a"]["rule"], events["b"]["kind"]), (4, "x", "guard"))

    def test_a_crash_that_left_a_partial_last_line_does_not_swallow_the_next_record(self):
        self.repository.append_events(self.work, [{"id": "a"}])
        with open(self.repository.get_path(self.work), "ab") as handle:
            handle.write(b'{"id": "broken')
        self.repository.append_events(self.work, [{"id": "c"}])
        self.assertEqual([event["id"] for event in ErrorEventRepository().read_events(self.work)], ["a", "c"])

    def test_an_unwritable_folder_returns_false(self):
        blocker = os.path.join(self.work, ".specs")
        open(blocker, "w").close()  # a FILE where the folder should be
        self.assertFalse(self.repository.append_events(self.work, [{"id": "a"}]))


if __name__ == "__main__":
    unittest.main()
