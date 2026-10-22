"""T8 (WAVE-13): the panel state carries `tasks` ({tasks, max_parallel}) only on the features whose status is `building`."""
import os

from .support_read import ReadTestCase

import task_waves  # noqa: E402  (the engine folder is on sys.path once the app package is imported)

TASKS = ("### T1: Model [P]\n**Type**: impl\n**Files**: src/a/model.py\n**Deps**: none\n**Done when**: ok\n\n"
         "### T2: Service [P]\n**Type**: impl\n**Files**: src/a/service.py\n**Deps**: none\n**Done when**: ok\n\n"
         "### T3: Wire it\n**Type**: wiring\n**Files**: src/a/wiring.py\n**Deps**: T1, T2\n**Done when**: ok\n")


class PanelTasksBlockTests(ReadTestCase):
    def write_tasks(self, slug, text=TASKS):
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "features", slug, "tasks.md"), text)

    def features(self):
        self.fixture.start()
        return {feature["id"]: feature for feature in self.state()["features"]}

    def test_the_building_feature_carries_its_tasks_in_wave_order(self):
        self.write_tasks("service-a")
        feature = self.features()["service-a"]
        self.assertEqual(feature["status"], "building")
        self.assertEqual(feature["tasks"]["max_parallel"], 3)
        self.assertEqual([(task["id"], task["wave"], task["status"]) for task in feature["tasks"]["tasks"]],
                         [("T1", 1, "pending"), ("T2", 1, "pending"), ("T3", 2, "pending")])
        self.assertEqual(feature["tasks"]["tasks"][2]["title"], "Wire it")

    def test_every_feature_with_a_tasks_md_carries_tasks_whatever_its_status(self):
        for slug in ("service-a", "service-b", "service-c", "service-d", "base"):
            self.write_tasks(slug)
        features = self.features()
        for slug in ("service-a", "base", "service-b", "service-c", "service-d"):
            self.assertEqual(features[slug]["tasks"]["source"], "tasks.md", slug)
            self.assertEqual(len(features[slug]["tasks"]["tasks"]), 3, slug)

    def test_features_without_a_tasks_md_carry_no_tasks_key_unless_building(self):
        features = self.features()
        for slug in ("base", "service-b", "service-c", "service-d"):
            self.assertNotIn("tasks", features[slug], slug)
        self.assertEqual(features["service-a"]["tasks"]["source"], "none")

    def test_each_task_carries_deps_files_and_the_parallel_mark(self):
        self.write_tasks("service-a")
        tasks = {task["id"]: task for task in self.features()["service-a"]["tasks"]["tasks"]}
        self.assertEqual((tasks["T1"]["deps"], tasks["T1"]["files"], tasks["T1"]["parallel"]), ([], ["src/a/model.py"], True))
        self.assertEqual((tasks["T3"]["deps"], tasks["T3"]["files"], tasks["T3"]["parallel"]), (["T1", "T2"], ["src/a/wiring.py"], False))

    def test_the_status_file_is_joined(self):
        self.write_tasks("service-a")
        task_waves.write_status(self.fixture.work, "service-a", "T1", "done")
        task_waves.write_status(self.fixture.work, "service-a", "T2", "running", "halfway")
        tasks = {task["id"]: task for task in self.features()["service-a"]["tasks"]["tasks"]}
        self.assertEqual((tasks["T1"]["status"], tasks["T2"]["status"], tasks["T2"]["note"], tasks["T3"]["status"]),
                         ("done", "running", "halfway", "pending"))

    def test_a_building_feature_without_tasks_md_has_an_empty_task_list(self):
        self.assertEqual(self.features()["service-a"]["tasks"]["tasks"], [])


if __name__ == "__main__":
    unittest.main()
