import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "prepare_task_bundle.py"
SPEC = importlib.util.spec_from_file_location("prepare_task_bundle", MODULE)
PREPARE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PREPARE)


class PrepareTaskBundleTests(unittest.TestCase):
    def test_replaces_statement_and_keeps_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template"
            (template / "environment").mkdir(parents=True)
            (template / "tests").mkdir()
            (template / "task.toml").write_text(
                'name = "old"\ndescription = "old"\nproblem_id = "M002"\n'
                'source_sha256 = "old"\nstatement_sha256 = "old"\n'
            )
            (template / "instruction.md").write_text("Read /app/readme.md")
            (template / "environment/Dockerfile").write_text("FROM base")
            (template / "environment/meta-data.md").write_text("private")
            (template / "repair-log.md").write_text("private")
            (template / "repair-record.json").write_text("{}")
            (template / "tests/test.sh").write_text("#!/bin/sh\n")
            statement = root / "task.md"
            statement.write_text("# Training-data conjecture\n")
            output = root / "out"
            manifest = PREPARE.prepare(template, statement, output, "training/task-1", "math-auth-1")

            self.assertEqual((output / "environment/readme.md").read_text(), statement.read_text())
            self.assertTrue((output / "tests/test.sh").exists())
            self.assertFalse((output / "environment/meta-data.md").exists())
            self.assertFalse((output / "repair-log.md").exists())
            task_toml = (output / "task.toml").read_text()
            self.assertIn('name = "training/task-1"', task_toml)
            self.assertIn('problem_id = "math-auth-1"', task_toml)
            self.assertEqual(manifest["statement_sha256"], PREPARE.sha256(statement))


if __name__ == "__main__":
    unittest.main()
