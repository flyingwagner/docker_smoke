import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import entry

BASELINE = json.loads((ROOT / "runtime-config.baseline.json").read_text())


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
                    "self-deepseek-v4.1-flash-1017", "https://example.test/v1/",
                )
            self.assertEqual(config["n_attempts"], 1)
            self.assertEqual(config["n_concurrent_trials"], 1)
            self.assertEqual(config["retry"]["max_retries"], 0)
            agent = config["agents"][0]
            self.assertEqual(agent["import_path"], "gewu_harbor.native_goal:NativeGoal")
            self.assertEqual(agent["kwargs"]["config"]["model_reasoning_summary"], "auto")
            self.assertEqual(agent["kwargs"]["config"]["web_search"], "live")
            self.assertEqual(config["verifier"]["env"]["JUDGE_MAX_TOKENS"], "98304")

    def test_endpoint_is_injected_into_selected_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {}, clear=False):
                config = entry.build_config(
                    Path(directory), Path(directory) / "task", Path(directory) / "out",
                    "self-deepseek-v4.1-flash-1017", "https://example.test/v1/",
                )
            agent = config["agents"][0]
            agent_config = agent["kwargs"]["config"]
            provider = agent_config["model_providers"][agent_config["model_provider"]]
            self.assertEqual(provider["base_url"], "https://example.test/v1")
            self.assertEqual(provider["env_key"], "MATRIXLLMROUTER_API_KEY")
            self.assertEqual(provider["wire_api"], "responses")

    def test_endpoint_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {}, clear=False):
                with self.assertRaises(ValueError):
                    entry.build_config(
                        Path(directory), Path(directory) / "task", Path(directory) / "out",
                        "self-deepseek-v4.1-flash-1017", "",
                    )

    def test_model_catalog_matches_by_slug_not_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "models" / "external-deepseek-v41-flash-1017.json").write_text(
                '{"models": [{"slug": "deepseek-v4.1-flash-1017"}]}'
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                config = entry.build_config(
                    root, root / "task", root / "out", "deepseek-v4.1-flash-1017",
                    "https://example.test/v1",
                )
            self.assertEqual(
                config["agents"][0]["kwargs"]["model_catalog"],
                str(root / "models" / "external-deepseek-v41-flash-1017.json"),
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                config = entry.build_config(
                    root, root / "task", root / "out", "unknown-model", "https://example.test/v1",
                )
            self.assertIsNone(config["agents"][0]["kwargs"]["model_catalog"])

    def test_baseline_declares_native_goal_with_current_sampling(self):
        agent = BASELINE["agents"][0]
        self.assertEqual(agent["import_path"], "gewu_harbor.native_goal:NativeGoal")
        self.assertEqual(agent["model_name"], "deepseek-v4.1-flash-1017")
        self.assertEqual(agent["kwargs"]["version"], "0.158.0")
        self.assertNotIn("response_proxy", agent["kwargs"])
        agent_config = agent["kwargs"]["config"]
        self.assertEqual(agent_config["model_reasoning_summary"], "auto")
        self.assertEqual(agent_config["model_reasoning_effort"], "max")
        self.assertEqual(agent_config["web_search"], "live")
        self.assertEqual(agent_config["model_context_window"], 1000000)


if __name__ == "__main__":
    unittest.main()
