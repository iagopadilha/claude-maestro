"""The delivery choices made before the worktree exists wait where run_loop.merge_pending_delivery reads them (internal/)."""
import os
import types
import unittest

import tests.engine.engine_env  # noqa: F401  (puts the engine on sys.path)
from harness_backend.services.loop_control_service import LoopControlService
import run_loop


class PendingDeliveryLocationTests(unittest.TestCase):
    def test_service_writes_the_pending_file_where_the_engine_merges_it(self):
        service = object.__new__(LoopControlService)
        context = types.SimpleNamespace(repo_path=os.path.join("some", "repo"))
        expected = os.path.join(context.repo_path, ".specs", "project", "loop", "internal", run_loop.DELIVERY_PENDING_NAME)
        self.assertEqual(service.pending_delivery_path(context), expected)


if __name__ == "__main__":
    unittest.main()
