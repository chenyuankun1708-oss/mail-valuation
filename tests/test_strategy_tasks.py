import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from valuation_app.web import StrategyTaskManager


class Completed:
    returncode = 0


class StrategyTaskManagerTest(unittest.TestCase):
    def test_fixed_subprocess_is_single_concurrency_and_persistent(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".runtime"))
            with patch("valuation_app.web.subprocess.run", return_value=Completed()) as run:
                manager = StrategyTaskManager(root)
                task_id, created = manager.submit()
                self.assertTrue(created)
                for _ in range(100):
                    if manager.get(task_id)["status"] != "running":
                        break
                    time.sleep(.01)
                self.assertEqual("ok", manager.get(task_id)["status"])
                command = run.call_args[0][0]
                self.assertEqual("strategy-lab", command[-1])
                self.assertEqual(os.path.join(root, "app.py"), command[-2])
            with open(os.path.join(root, ".runtime", "strategy-tasks.json"),
                      "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(task_id, saved["tasks"][0]["id"])
            self.assertNotIn("stdout", saved["tasks"][0])

    def test_existing_running_task_is_reused(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".runtime"))
            manager = StrategyTaskManager(root)
            manager.tasks["rebuild-existing"] = {"id": "rebuild-existing", "status": "running",
                                                   "created_at": time.time()}
            task_id, created = manager.submit()
            self.assertEqual("rebuild-existing", task_id)
            self.assertFalse(created)


if __name__ == "__main__":
    unittest.main()
