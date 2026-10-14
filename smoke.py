"""Verify Python packages, basic computation, Codex CLI, and writable output."""

import importlib
import importlib.metadata
import json
import os
import platform
import subprocess
from pathlib import Path


PACKAGES = {
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "requests": "requests",
    "httpx": "httpx",
    "PyYAML": "yaml",
    "tqdm": "tqdm",
    "rich": "rich",
    "pytest": "pytest",
}


def command_version(*args):
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def main():
    output_value = os.environ.get("PLATFORM_OUTPUT_PATH")
    if not output_value:
        raise RuntimeError("Set PLATFORM_OUTPUT_PATH to an existing writable directory")
    output = Path(output_value)
    if not output.is_dir():
        raise RuntimeError("PLATFORM_OUTPUT_PATH must be an existing directory")

    metrics = {"python_version": platform.python_version(), "uid": os.getuid()}
    for distribution, module in PACKAGES.items():
        importlib.import_module(module)
        metrics[f"package_{distribution.lower()}"] = importlib.metadata.version(distribution)

    import numpy as np
    import pandas as pd
    from scipy.linalg import solve

    values = np.arange(1, 1001, dtype=np.int64)
    total = int(pd.DataFrame({"value": values})["value"].sum())
    squares = int(np.dot(values, values))
    solution = solve([[3.0, 1.0], [1.0, 2.0]], [9.0, 8.0])
    if total != 500500 or squares != 333833500 or not np.allclose(solution, [2.0, 3.0]):
        raise RuntimeError("Numerical smoke check failed")

    metrics.update({
        "sum": total,
        "sum_squares": squares,
        "node_version": command_version("node", "--version"),
        "npm_version": command_version("npm", "--version"),
        "codex_version": command_version("codex", "--version"),
        "git_version": command_version("git", "--version"),
        "ok": 1,
    })
    artifact = output / "hello.txt"
    message = "Python packages and Codex CLI are ready.\n"
    artifact.write_text(message, encoding="utf-8")
    if artifact.read_text(encoding="utf-8") != message:
        raise RuntimeError("Output-file round trip failed")

    metrics_path = output / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    if json.loads(metrics_path.read_text(encoding="utf-8")) != metrics:
        raise RuntimeError("Metrics round trip failed")
    print(json.dumps(metrics, indent=2), flush=True)


if __name__ == "__main__":
    if os.environ.get("RUN_MODE") == "codex-api":
        from api_smoke import main as api_main

        api_main()
    else:
        main()
