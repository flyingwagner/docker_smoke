"""Run one Codex task through an existing, runtime-injected gateway credential."""

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path


def main():
    output = Path(os.environ["PLATFORM_OUTPUT_PATH"])
    if not output.is_dir():
        raise RuntimeError("PLATFORM_OUTPUT_PATH must already exist")
    source = Path(__file__).with_name("codex-config.toml")
    config_bytes = source.read_bytes()
    config = tomllib.loads(config_bytes.decode())
    provider = config["model_providers"][config["model_provider"]]
    key = os.environ.get(provider["env_key"]) or os.environ.get("PLATFORM_ENDPOINT_KEY")
    if not key:
        raise RuntimeError("No gateway credential was injected into the task")

    metrics = {
        "ok": 0,
        "provider": config["model_provider"],
        "model": config["model"],
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "uid": os.getuid(),
    }

    def save_metrics():
        (output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    def clean(value):
        return value.replace(key, "[REDACTED]")

    expected = os.environ.get("EXPECTED_API_KEY_SHA256")
    if expected:
        matches = hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), expected)
        metrics["credential_matches_local"] = int(matches)
        if not matches:
            save_metrics()
            print(json.dumps(metrics), flush=True)
            raise RuntimeError("The platform credential differs from the existing local gateway key; no model request was sent")

    endpoint = os.environ.get("PLATFORM_ENDPOINT_URL")
    model = os.environ.get("PLATFORM_ENDPOINT_MODEL")
    if endpoint and endpoint.rstrip("/") != provider["base_url"].rstrip("/"):
        raise RuntimeError("Injected endpoint does not match the existing provider configuration")
    if model and model != config["model"]:
        raise RuntimeError("Injected model does not match the requested model")

    workspace = output / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    state_directory = workspace / ".codex-home"
    state_directory.mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(source, state_directory / "config.toml")
    environment = os.environ.copy()
    environment[provider["env_key"]] = key
    # Same supported Codex state-directory setting used by the source runtime.
    environment["CODEX_HOME"] = str(state_directory)

    prompt = (
        "Use Python to calculate 37 * 43 and create answer.json in the current "
        "working directory with a single JSON field named product containing "
        "the result. Run the calculation using the shell tool. Do not read "
        "environment variables or files outside this working directory. "
        "After writing the file, reply only DONE.\n"
    )
    (output / "prompt.txt").write_text(prompt)
    command = ["codex", "exec", "--skip-git-repo-check", "--json", "-"]
    (output / "request-metadata.json").write_text(json.dumps({
        "command": command,
        "cwd": str(workspace),
        "model": config["model"],
        "provider": provider,
        "model_reasoning_summary": config["model_reasoning_summary"],
        "config_sha256": metrics["config_sha256"],
    }, indent=2) + "\n")
    save_metrics()
    print("Starting Codex gateway task for " + config["model"], flush=True)
    result = subprocess.run(
        command, input=prompt, cwd=workspace, env=environment,
        capture_output=True, text=True,
    )
    stdout, stderr = clean(result.stdout), clean(result.stderr)
    (output / "codex-events.jsonl").write_text(stdout)
    (output / "codex-stderr.log").write_text(stderr)
    metrics["codex_returncode"] = result.returncode
    if result.returncode:
        save_metrics()
        print((stdout + "\n" + stderr)[-6000:], flush=True)
        raise RuntimeError("Codex gateway task failed")

    artifact = workspace / "answer.json"
    answer = json.loads(artifact.read_text())
    if answer != {"product": 1591}:
        save_metrics()
        raise RuntimeError("Codex calculation artifact did not match the expected result")
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    calls = sum(
        event.get("type") == "item.completed"
        and event.get("item", {}).get("type") == "command_execution"
        for event in events
    )
    if not calls:
        save_metrics()
        raise RuntimeError("No completed shell tool call was recorded")
    for event in events:
        if event.get("type") == "turn.completed":
            for name, value in event.get("usage", {}).items():
                if isinstance(value, (int, float)):
                    metrics["usage_" + name] = value
    metrics.update({"ok": 1, "product": answer["product"], "completed_shell_calls": calls})
    save_metrics()
    print(json.dumps(metrics, indent=2), flush=True)


if __name__ == "__main__":
    main()
