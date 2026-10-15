FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS codex_tools
RUN npm install --global @openai/codex@0.162.1 \
    && npm cache clean --force

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates curl git jq ripgrep libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=codex_tools /usr/local/bin/node /usr/local/bin/node
COPY --from=codex_tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --root-user-action=ignore -r /app/requirements.txt \
    && python -m pip check \
    && node --version \
    && codex --version

COPY smoke.py /app/smoke.py
RUN groupadd --gid 2001 runner \
    && useradd --uid 2001 --gid 2001 --create-home --shell /bin/bash runner
USER 2001:2001
CMD ["python", "/app/smoke.py"]
