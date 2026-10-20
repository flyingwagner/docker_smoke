"""Prepare a Harbor task bundle from a task shell and a training-data task.md."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


COPY_FILES = (
    Path("task.toml"),
    Path("instruction.md"),
    Path("environment/Dockerfile"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewrite_task_toml(text: str, task_name: str, problem_id: str, statement_hash: str) -> str:
    replacements = {
        r'(?m)^name = "[^"]*"$': f'name = "{task_name}"',
        r'(?m)^description = "[^"]*"$': f'description = "Training-data conjecture: {task_name}"',
        r'(?m)^problem_id = "[^"]*"$': f'problem_id = "{problem_id}"',
        r'(?m)^source_sha256 = "[^"]*"$': f'source_sha256 = "{statement_hash}"',
        r'(?m)^statement_sha256 = "[^"]*"$': f'statement_sha256 = "{statement_hash}"',
    }
    for pattern, replacement in replacements.items():
        text, count = re.subn(pattern, replacement, text, count=1)
        if count != 1:
            raise ValueError(f"task.toml missing expected metadata line: {pattern}")
    return text


def prepare(template: Path, statement: Path, output: Path, task_name: str, problem_id: str) -> dict:
    template = template.resolve()
    statement = statement.resolve()
    output = output.resolve()
    if not (template / "task.toml").is_file():
        raise ValueError(f"template task.toml not found: {template}")
    if not statement.is_file() or not statement.read_text(encoding="utf-8").strip():
        raise ValueError(f"statement is missing or empty: {statement}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    statement_hash = sha256(statement)
    copied: list[dict[str, str]] = []
    for relative in COPY_FILES:
        source = template / relative
        if not source.is_file():
            raise ValueError(f"template file missing: {source}")
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding="utf-8")
        if relative == Path("task.toml"):
            content = rewrite_task_toml(content, task_name, problem_id, statement_hash)
            destination.write_text(content, encoding="utf-8")
        else:
            shutil.copy2(source, destination)
        copied.append({"path": relative.as_posix(), "sha256": sha256(destination)})

    tests_source = template / "tests"
    if not tests_source.is_dir():
        raise ValueError(f"template tests directory missing: {tests_source}")
    for source in sorted(tests_source.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(template)
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append({"path": relative.as_posix(), "sha256": sha256(destination)})

    readme = output / "environment/readme.md"
    readme.write_bytes(statement.read_bytes())
    copied.append({"path": "environment/readme.md", "sha256": sha256(readme)})
    manifest = {
        "format": "harbor-training-data-task-v1",
        "task_name": task_name,
        "problem_id": problem_id,
        "statement_sha256": statement_hash,
        "statement_source": str(statement),
        "template_source": str(template),
        "copied_files": sorted(copied, key=lambda item: item["path"]),
        "excluded_template_files": [
            "environment/meta-data.md",
            "repair-log.md",
            "repair-record.json",
        ],
    }
    (output / "training-data-task-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--statement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--problem-id", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.template, args.statement, args.output, args.task_name, args.problem_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
