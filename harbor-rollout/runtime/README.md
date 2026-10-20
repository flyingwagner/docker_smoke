# Harbor rollout runtime (BenchmarkJob)

This runtime executes one Harbor task bundle inside the platform's
`BenchmarkJob` container. The outer BenchmarkJob is the isolation boundary;
the Harbor environment backend maps task paths and commands into that container
and never starts Docker or Kubernetes children.

The runtime is built from the H200-local `qrepair-agent:codex-0.158.0` image
(digest `sha256:7a165dff0186ca9b2b59deaf14b1c3da4e01d3b6011f1ab1d5058eba2c3749d4`),
installs Harbor 0.23, and adds the native Goal driver used by the Gewu
benchmark. For self-hosted DeepSeek 1017, the runtime's local bridge marks
Responses requests for the gateway's Chat Completions backend; it does not
change the model's generation parameters. It is a new runtime; it does not use
the `gewu-bench-runtime` Recipe or its coordinator entrypoint.

## Input contract

The BenchmarkJob dataset may be the task directory itself or contain one under
`task/`. The `gewu-conjecture-bench/gewu-mXXX` directories are used only as the
Harbor-format and verifier-contract reference. They are not rollout inputs.
The actual rollout task must come from the training-data conjecture group and
have this shape:

```text
task/
  task.toml
  instruction.md
  environment/
    Dockerfile
    readme.md
  tests/
    test.sh
    judge_llm.py
    judge_stream_reader.py
```

The runtime image supplies Harbor and the Goal adapter. The task bundle
supplies its own `task.toml`, instruction and verifier scripts; no top-level
repository `scripts/` directory is copied into the task. The task is read-only.
The attached environment emulates the task Dockerfile's solver-visible
`environment/readme.md` copy and does not expose `meta-data.md`, repair logs or
repair records to the solver. The endpoint subject supplies
`PLATFORM_ENDPOINT_URL`, `PLATFORM_ENDPOINT_MODEL` and
`PLATFORM_ENDPOINT_KEY`; the key is never written to task output.
The platform-assigned Unix identity is used throughout, including when the task
declares the logical user `agent`. Workspaces, native sessions, and verifier
outputs live beneath `PLATFORM_OUTPUT_PATH/sandboxes/<session>/`. The image's
`/app`, `/logs`, `/tests`, `/solution`, `/harbor`, and `/installed-agent` symlinks
resolve through `/tmp/harbor-active`, which the backend creates for this trial.
It refuses to reuse an anchor belonging to a different trial. Original task
instructions, Goal paths, and shell commands remain unchanged. The verifier
receives the same endpoint model. The preserved solver/verifier
configuration is declared in [runtime-config.baseline.json](runtime-config.baseline.json)
and copied to the output beside the resolved Harbor configuration. It includes
the existing reasoning controls, 300-turn/12-hour solver cap, 7,500-second
verifier cap and verifier request controls. No Harbor trial retries are enabled.

Settings are optional and are recorded in `harbor-job-config.json`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `HARBOR_TASK_RELATIVE` | `task` | Task directory under the dataset |
| `HARBOR_CODEX_MODEL` | endpoint model | Codex model slug/alias |
| `HARBOR_ATTEMPTS` | `1` | Must remain 1; one attempt per BenchmarkJob |
| `HARBOR_CONCURRENCY` | `1` | Must remain 1; parallelism is across BenchmarkJobs |
| `HARBOR_MAX_TURNS` | `300` | Native Goal turn cap |
| `HARBOR_SOLVER_TIMEOUT_SEC` | `43200` | Native Goal wall-clock cap |

## Output contract

The runtime writes `metrics.json` and fails if Harbor reports an execution error
or unless the run produced all three
training inputs: at least one ATIF `trajectory.json`, one native Codex session
JSONL and one verifier `reward.txt`. It also preserves Harbor's raw job tree,
`harbor-job-config.json`, `harbor-job-result.json`, `output-inventory.json`,
and a redacted error log when startup or execution fails.

Submit one BenchmarkJob per task/attempt. The first smoke and all batch jobs use
`HARBOR_ATTEMPTS=1` and `HARBOR_CONCURRENCY=1`; batch concurrency is managed by
the submitter across separate platform jobs.

## Build and submit

Use [recipe.example.yaml](recipe.example.yaml) to build the image from a
reviewed matrixinfra revision. After the Recipe is `Built`, copy its resolved
digest into the BenchmarkJob registration and submit the JSON shape in
[benchmarkjob.example.json](benchmarkjob.example.json). The platform's online
manual requires the Harbor task bundle to be staged as a Dataset before this
submission.
