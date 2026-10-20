import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import entry


class ContractTests(unittest.TestCase):
    def test_task_path_stays_inside_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory)
            task = dataset / "task"
            task.mkdir()
            (task / "task.toml").write_text("schema_version = '1.0'\n")
            with mock.patch.dict(os.environ, {}, clear=False):
                self.assertEqual(entry.task_path(dataset), task)

    def test_task_path_rejects_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory)
            with mock.patch.dict(os.environ, {"HARBOR_TASK_RELATIVE": "../task"}, clear=False):
                with self.assertRaises(ValueError):
                    entry.task_path(dataset)

    def test_dataset_can_be_task_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory)
            (dataset / "task.toml").write_text("schema_version = '1.3'\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(entry.task_path(dataset), dataset)

    def test_config_is_single_trial_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {}, clear=False):
                config = entry.build_config(
                    Path(directory), Path(directory) / "task", Path(directory) / "out",
                    "deepseek-v4.1-flash",
                )
            self.assertEqual(config["n_attempts"], 1)
            self.assertEqual(config["n_concurrent_trials"], 1)
            self.assertEqual(config["retry"]["max_retries"], 0)
            self.assertEqual(config["agents"][0]["kwargs"]["config"]["model_reasoning_effort"], "max")
            self.assertEqual(config["verifier"]["env"]["JUDGE_MAX_TOKENS"], "98304")
            self.assertEqual(
                config["agents"][0]["import_path"], "codex_goal_deepseek:CodexGoal"
            )


if __name__ == "__main__":
    unittest.main()
