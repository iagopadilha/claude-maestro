"""Thread that advances the demo story every few seconds (scaled by --speed) while the app serves the panel.

It holds `LOOP.lock` with its own PID while a turn runs (the panel shows `running`), waits on the loop's question for a
while (an answer clicked in the panel, recorded by the engine stub, ends the wait early), and with `loop_forever` restarts
the story after it ends. Never spawns `claude`.
"""
import os
import threading
import time

from ..repositories.loop_file_repository import loop_state
from .demo_story_catalog import SEEDED_TURNS

STEP_SECONDS = 3.0
DONE_HOLD_SECONDS = 12.0
ANSWER_POLL_SECONDS = 0.25
ANSWER_FILE = "demo-answer.txt"


def compute_story_seconds(turn_plans, speed=1.0):
    """Seconds one story cycle takes from the first animated step to `done` (steps x STEP_SECONDS + the question hold)."""
    steps = sum(len(steps) for number, steps in turn_plans if number > SEEDED_TURNS)
    holds = sum(action.get("hold_seconds", 0) for number, steps in turn_plans if number > SEEDED_TURNS
                for step in steps for action in step if action["kind"] == "wait_for_answer")
    return (steps * STEP_SECONDS + holds) / speed


class DemoAnimator(threading.Thread):
    def __init__(self, seeder, player, log, speed=1.0, loop_forever=False):
        super().__init__(name="demo-animator", daemon=True)
        self.seeder = seeder
        self.player = player
        self.log = log
        self.speed = max(0.05, float(speed or 1.0))
        self.loop_forever = loop_forever
        self.stop_event = threading.Event()
        self.player.keeps_lock = True
        self.player.answer_provider = self.wait_for_answer

    def scaled(self, seconds):
        return seconds / self.speed

    def stop(self):
        self.stop_event.set()

    def answer_file(self):
        return loop_state.loop_path(self.seeder.repo_dir, ANSWER_FILE)

    def wait_for_answer(self, action):
        """Holds the question for `hold_seconds` (scaled); an answer clicked in the panel ends the wait and wins."""
        deadline = time.monotonic() + self.scaled(action.get("hold_seconds", 0))
        while time.monotonic() < deadline and not self.stop_event.is_set():
            clicked = self.read_clicked_answer()
            if clicked:
                return clicked
            self.stop_event.wait(ANSWER_POLL_SECONDS)
        return self.read_clicked_answer() or action["answer"]

    def read_clicked_answer(self):
        path = self.answer_file()
        if not os.path.isfile(path):
            return None
        text = loop_state.read(path).strip()
        try:
            os.remove(path)
        except OSError:
            pass
        return text or None

    def run(self):
        try:
            while not self.stop_event.is_set():
                if not self.player.is_finished():
                    self.player.apply_next_step()
                    self.stop_event.wait(self.scaled(STEP_SECONDS))
                    continue
                self.log.info("demo_story_done", turns=self.player.get_completed_turns())
                if not self.loop_forever:
                    break
                self.stop_event.wait(self.scaled(DONE_HOLD_SECONDS))
                if self.stop_event.is_set():
                    break
                self.player = self.seeder.reset_story(keeps_lock=True, answer_provider=self.wait_for_answer)
                self.log.info("demo_story_restarted", generation=self.seeder.generation)
        except Exception as failure:  # the demo never takes the app down; the panel keeps the last state
            self.log.error("demo_animator_failed", type=type(failure).__name__, detail=str(failure)[:300])
        finally:
            self.player.release_lock()
