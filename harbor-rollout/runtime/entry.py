"""Run one Harbor task bundle inside a BenchmarkJob container."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import traceback

CONTRACT = "benchmark-harbor-rollout-v1"
BASELINE_PATH = Path(__file__).with_name("runtime-config.baseline.json")


def redact(value: str) -> str:
    from attached_environment import redact as redact_value

    return redact_value(value)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default))
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def task_path(dataset: Path) -> Path:
    relative = os.environ.get("HARBOR_TASK_RELATIVE")
    if relative is None:
        relative = "." if (dataset / "task.toml").is_file() else "task"
    relative = relative.strip()
    candidate = (dataset / relative).resolve()
    if not candidate.is_relative_to(dataset.resolve()):
        raise ValueError("HARBOR_TASK_RELATIVE escapes the dataset")
    if not (candidate / "task.toml").is_file():
        raise ValueError(f"Harbor task.toml not found under {candidate}")
    return candidate


def resolve_model_catalog(source: Path, model: str) -> str | None:
    """Match a catalog by its slug, not its filename (v41 files lack the dot)."""
    directory = source / "models"
    if not directory.is_dir():
        return None
    for catalog in sorted(directory.glob("*.json")):
        try:
            document = json.loads(catalog.read_text())
        except (OSError, ValueError):
            continue
        entries = document.get("models") if isinstance(document, dict) else None
        if isinstance(entries, list) and any(
            isinstance(entry, dict) and entry.get("slug") == model for entry in entries
        ):
            return str(catalog)
    return None


def build_config(source: Path, task: Path, output: Path, model: str, endpoint: str) -> dict:
    config = json.loads(BASELINE_PATH.read_text())
    if env_int("HARBOR_ATTEMPTS", 1) != 1 or env_int("HARBOR_CONCURRENCY", 1) != 1:
        raise ValueError("Submit one BenchmarkJob per task and attempt; internal concurrency must be 1")
    agent = config["agents"][0]
    kwargs = agent["kwargs"]
    agent_config = kwargs["config"]
    provider = agent_config["model_providers"][agent_config["model_provider"]]
    if not endpoint:
        raise ValueError("The NativeGoal provider requires the platform endpoint URL")
    provider["base_url"] = endpoint.rstrip("/")
    kwargs["model_catalog"] = resolve_model_catalog(source, model)
    agent["model_name"] = model
    kwargs["max_turns"] = env_int("HARBOR_MAX_TURNS", 300)
    kwargs["total_timeout_sec"] = env_int("HARBOR_SOLVER_TIMEOUT_SEC", 43200)
    config["verifier"]["env"]["JUDGE_MODEL"] = model
    config.update({
        "job_name": f"harbor-rollout-{output.name}",
        "jobs_dir": str(output / "harbor-jobs"),
        "tasks": [{"path": str(task)}],
        "environment": {
            "import_path": "attached_environment:AttachedJobEnvironment",
            "force_build": False,
            "delete": False,
            "cpu_enforcement_policy": "ignore",
            "memory_enforcement_policy": "ignore",
        },
    })
    return config


def inventory(output: Path) -> list[dict[str, object]]:
    result = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            result.append({"path": str(path.relative_to(output)), "bytes": path.stat().st_size})
    return result


def main() -> int:
    output = Path(os.environ["PLATFORM_OUTPUT_PATH"]).resolve()
    dataset = Path(os.environ["PLATFORM_DATASET_PATH"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    task = task_path(dataset)
    endpoint = os.environ.get("PLATFORM_ENDPOINT_URL", "").strip()
    key = os.environ.get("PLATFORM_ENDPOINT_KEY", "").strip()
    if not endpoint or not key:
        raise RuntimeError("Harbor rollout requires a platform endpoint subject")
    model = os.environ.get("HARBOR_CODEX_MODEL", os.environ.get("PLATFORM_ENDPOINT_MODEL", "")).strip()
    if not model:
        raise RuntimeError("HARBOR_CODEX_MODEL or PLATFORM_ENDPOINT_MODEL is required")

    os.environ["OPENAI_API_KEY"] = key
    os.environ["OPENAI_BASE_URL"] = endpoint
    os.environ["MATRIXLLMROUTER_API_KEY"] = key
    os.environ.setdefault("JUDGE_MODEL", model)
    os.environ["PYTHONPATH"] = os.pathsep.join((str(source), os.environ.get("PYTHONPATH", "")))

    config = build_config(source, task, output, model, endpoint)
    write_json(output / "runtime-config-baseline.json", json.loads(BASELINE_PATH.read_text()))
    write_json(output / "harbor-job-config.json", {
        **config,
        "endpoint": endpoint,
        "model": model,
        "credential": "platform-managed endpoint key",
        "task": str(task),
        "contract": CONTRACT,
    })

    metrics = {
        "runtime_contract": CONTRACT,
        "harbor_version": importlib.metadata.version("harbor"),
        "model": model,
        "task": str(task.relative_to(dataset)),
        "attempts": config["n_attempts"],
        "concurrency": config["n_concurrent_trials"],
        "ok": 0,
    }
    run_error = None
    try:
        from harbor.job import Job
        from harbor.models.job.config import JobConfig

        async def execute():
            job = await Job.create(JobConfig.model_validate(config))
            return await job.run()

        result = asyncio.run(execute())
        write_json(output / "harbor-job-result.json", json.loads(redact(result.model_dump_json())))
        stats = result.stats
        metrics.update({
            "completed_trials": stats.n_completed_trials,
            "errored_trials": stats.n_errored_trials,
        })
        for name in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd"):
            value = getattr(stats, name, None)
            if value is not None:
                metrics[name] = value
        if (stats.n_completed_trials != config["n_attempts"]
                or stats.n_errored_trials or stats.n_running_trials
                or stats.n_pending_trials or stats.n_cancelled_trials):
            raise RuntimeError("Harbor did not finish all requested trials without execution errors")
    except BaseException as error:
        run_error = error
        (output / "harbor-run-error.log").write_text(
            redact("".join(traceback.format_exception(error)))
        )

    files = inventory(output)
    trajectories = [item for item in files if str(item["path"]).endswith("trajectory.json")]
    sessions = [
        item for item in files
        if str(item["path"]).endswith(".jsonl") and "/sessions/" in str(item["path"])
    ]
    rewards = []
    for path in (output / "harbor-jobs").rglob("reward.txt"):
        try:
            rewards.append(float(path.read_text().strip()))
        except (OSError, ValueError):
            pass
    metrics.update({
        "atif_trajectory_files": len(trajectories),
        "native_session_files": len(sessions),
        "reward_files": len(rewards),
        "output_files": len(files),
    })
    if rewards:
        metrics["reward_min"] = min(rewards)
    metrics["ok"] = int(run_error is None and bool(trajectories) and bool(sessions) and bool(rewards))
    write_json(output / "output-inventory.json", files)
    write_json(output / "metrics.json", metrics)
    if run_error is not None:
        raise RuntimeError("Harbor rollout failed; inspect harbor-run-error.log") from run_error
    if not metrics["ok"]:
        raise RuntimeError("Harbor rollout did not produce ATIF, native session and reward")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        destination = os.environ.get("PLATFORM_OUTPUT_PATH")
        if destination:
            write_json(Path(destination) / "task-error.json", {
                "type": type(error).__name__,
                "detail": redact(str(error)),
            })
        print(redact(f"harbor rollout failed: {type(error).__name__}: {error}"), flush=True)
        raise SystemExit(2)
