"""User activity and app busyness, for the idle shutdown.

The app only shuts down on its own when NO busy provider is busy (loop running, chat/roadmap in progress, relaunched
loop alive) AND there was no activity for `idle_hours`. `idle_hours == 0` = never shuts down.
"""
import threading
import time
from typing import Protocol


class IBusyProvider(Protocol):
    def is_busy(self) -> bool:
        """True while there is work that prevents the shutdown (e.g. a loop running in some open project)."""


class ActivityMonitor:
    def __init__(self, idle_hours, on_idle):
        self.idle_seconds = float(idle_hours) * 3600
        self.on_idle = on_idle
        self.busy_providers = []
        self.last_activity = time.time()
        self.stop = threading.Event()

    def add_busy_provider(self, provider):
        self.busy_providers.append(provider)

    def record_activity(self):
        self.last_activity = time.time()

    def is_any_provider_busy(self):
        for provider in self.busy_providers:
            try:
                if provider.is_busy():
                    return True
            except Exception:
                return True  # when in doubt, do not shut down
        return False

    def seconds_until_shutdown(self):
        if self.idle_seconds <= 0 or self.is_any_provider_busy():
            return None
        return max(0, int(self.idle_seconds - (time.time() - self.last_activity)))

    def watch_idle_in_background(self):
        if self.idle_seconds <= 0:
            return None
        watcher = threading.Thread(target=self._watch, daemon=True)
        watcher.start()
        return watcher

    def _watch(self):
        while not self.stop.wait(0.5):
            if self.is_any_provider_busy():
                self.record_activity()
            elif time.time() - self.last_activity >= self.idle_seconds:
                self.on_idle()
                return
