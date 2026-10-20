# Training-data Harbor rollout on Matrixinfra

This image reconstructs the H200 qrepair-agent:codex-0.158.0 environment using the original Dockerfile and installed-package inventory. The Codex executable hashes and solver Python package versions must match H200 at build time. Debian base packages are rebuilt from a digest-pinned upstream image; this is not a byte-identical copy of the original Docker image.

The solver keeps Python 3.11.2; Harbor 0.23.0 runs in a separate Python 3.12.12 environment, matching the H200 host harness. One platform BenchmarkJob runs one training-data task and saves native sessions, ATIF trajectory, Goal state, verifier outputs and workspace under PLATFORM_OUTPUT_PATH. Fixed /app and /logs paths point into that persisted workspace.

The task input comes only from the training-data conjecture repository. Benchmark task statements are not bundled in this image. The generic Harbor shell is provided as a versioned Dataset. Model credentials are supplied at runtime, never in the image.

Build through a project Recipe using repository https://github.com/flyingwagner/docker_smoke.git, the full branch commit, and Dockerfile path harbor-rollout/image-source/Dockerfile.H200-base. No workstation image build is required.

Generation, retry and timeout baselines are in runtime/runtime-config.baseline.json. The selected model is self-deepseek-v4.1-flash-1017. Platform integration acceptance is separate from image build success.
