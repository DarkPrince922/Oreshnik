# The agent container: Codex CLI behind a tiny HTTP service. It is the sandbox — it gets no Docker socket, no database,
# no Redis and none of the panel's secrets. Only a volume for its own working directories and a network route to the model.
#
# NOT built in the environment this was written in (no Docker daemon there): the Codex part mirrors a verified
# `npm i @openai/codex@0.169.0`, agentd was run with uvicorn on Python 3.13.
FROM node:22-slim

ARG CODEX_VERSION=0.169.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv git curl ca-certificates openssh-client build-essential \
    && rm -rf /var/lib/apt/lists/*

# `codex` is a thin JS launcher; the real binary is a separate package (@openai/codex-linux-x64 / -arm64) that npm installs as an
# OPTIONAL dependency — and silently skips when it cannot fetch it (network trouble, `omit=optional`), leaving an image that builds
# fine and then fails with "Missing optional dependency … Reinstall Codex". So the platform package is named explicitly, optional
# dependencies are forced on, and the build runs `codex --version`: a Codex that cannot start now fails the BUILD, not the first chat.
RUN set -eux; \
    case "$(uname -m)" in x86_64) ARCH=x64 ;; aarch64) ARCH=arm64 ;; *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;; esac; \
    mkdir -p /opt/codex && cd /opt/codex && npm init -y >/dev/null; \
    npm install --include=optional --no-audit --no-fund \
        "@openai/codex@${CODEX_VERSION}" "@openai/codex-linux-${ARCH}@npm:@openai/codex@${CODEX_VERSION}-linux-${ARCH}"; \
    ln -s /opt/codex/node_modules/.bin/codex /usr/local/bin/codex; \
    codex --version; \
    npm cache clean --force

WORKDIR /srv
COPY requirements.txt .
RUN python3 -m venv /opt/agentd && /opt/agentd/bin/pip install --no-cache-dir -r requirements.txt
COPY main.py .

# The image's own `node` user (uid 1000) runs everything; the volume is its to write.
RUN mkdir -p /data/agent && chown node:node /data/agent
USER node
ENV AGENT_DATA_DIR=/data/agent
EXPOSE 8100
CMD ["/opt/agentd/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
