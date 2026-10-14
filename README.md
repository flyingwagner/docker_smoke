# docker_smoke

A CPU smoke-test image with Python 3.12, Node.js 24, and Codex CLI 0.162.1.

Includes NumPy, pandas, SciPy, requests, HTTPX, PyYAML, tqdm, Rich, pytest,
and the command-line tools Git, curl, jq, and ripgrep. Both base images use
immutable SHA-256 digests. Direct Python dependencies and Codex are version
pinned; transitive Python dependencies and Debian packages are resolved at build time.

## Build and run

```bash
docker build -t docker-smoke:local .
mkdir -p output
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --network=none \
  -e PLATFORM_OUTPUT_PATH=/output \
  -v "$PWD/output:/output" \
  docker-smoke:local
cat output/metrics.json
```

The default command imports every Python package, checks NumPy/pandas/SciPy
calculations, runs `node --version`, `npm --version`, `codex --version`, and
`git --version`, then writes `hello.txt` and a flat `metrics.json` to the existing
directory specified by `PLATFORM_OUTPUT_PATH`. Success means exit code 0 and
`"ok": 1` in the metrics. The image defaults to UID/GID 2001 and also supports a
runtime-supplied non-root UID.

## Use the installed tools

Override the default smoke command to inspect a tool or start a shell:

```bash
docker run --rm docker-smoke:local codex --version
docker run --rm -it docker-smoke:local bash
```

Codex is installed using the [official npm installation method](https://developers.openai.com/codex/cli/).
The smoke test checks that the CLI starts; it does not sign in or make model calls.
Actual Codex tasks need authentication, network access, and writable workspace
and user configuration directories supplied at runtime. No credentials or user
configuration are included in the image.

## Codex gateway smoke test

Set `RUN_MODE=codex-api` to run a small agent task through MatrixLLMRouter using
the included provider configuration and the `deepseek-v4.1-flash` model. The
configuration retains the existing gateway workaround `model_reasoning_summary
= "none"`; no sampling settings or reasoning effort are added.

Supply the gateway credential at runtime using `MATRIXLLMROUTER_API_KEY` or the
platform-managed `PLATFORM_ENDPOINT_KEY`. An optional `EXPECTED_API_KEY_SHA256`
checks that the platform credential matches an existing local credential before
any model call. Only the fingerprint goes in the task specification.

The test creates a writable workspace and Codex state directory under
`PLATFORM_OUTPUT_PATH`, asks Codex to calculate 37 times 43 using Python, and
checks the resulting `answer.json` and recorded shell tool call. It saves
redacted event logs and returns flat metrics, including `ok`, the model,
Codex version, and token usage. This test makes real model requests.

## Platform builds

Use this repository's HTTPS clone URL, a full 40-character commit ID, and
`Dockerfile` as the Dockerfile path. The Dockerfile uses digest-pinned base images
and a named prior stage for `COPY --from`; it does not use dynamic base images,
Dockerfile heredocs, or build mount directives.

For a first run, request 0 GPUs, 1 CPU, 1 GiB RAM, and a 120-second timeout.
The platform should supply the writable `PLATFORM_OUTPUT_PATH` directory.
