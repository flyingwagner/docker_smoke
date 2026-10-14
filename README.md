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

## Platform builds

Use this repository's HTTPS clone URL, a full 40-character commit ID, and
`Dockerfile` as the Dockerfile path. The Dockerfile uses digest-pinned base images
and a named prior stage for `COPY --from`; it does not use dynamic base images,
Dockerfile heredocs, or build mount directives.

For a first run, request 0 GPUs, 1 CPU, 1 GiB RAM, and a 120-second timeout.
The platform should supply the writable `PLATFORM_OUTPUT_PATH` directory.
