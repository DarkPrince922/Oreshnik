# The agent container: Codex CLI behind a tiny HTTP service. It is the sandbox — it gets no Docker socket, no database,
# no Redis and none of the panel's secrets. Only a volume for its own working directories and a network route to the model.
#
# NOT built in the environment this was written in (no Docker daemon there): the Codex part mirrors a verified
# `npm i @openai/codex@0.169.0`, agentd was run with uvicorn on Python 3.13.
FROM node:22-slim

ARG CODEX_VERSION=0.169.0
# Baseline toolset for a coding/testing agent — authorized security testing and development only, never against systems you do not
# own or have written permission to test (see docs/ARCHITECTURE.md §26). Deliberately apt-only, bookworm's main repo, no backports:
#   dev:     jq, ripgrep (rg), httpie (http), zip/unzip, sqlite3, dnsutils (dig), openssl, less
#   network: nmap, netcat-openbsd (nc), whois, traceroute, nikto
# An admin can ADD more apt packages without touching this file — only AGENT_EXTRA_PACKAGES in .env, which goes straight into
# the same `apt-get install` below and needs a rebuild (`docker compose build agent`) to take effect, nothing lighter: the image
# is immutable at runtime (read-only root filesystem, cap_drop: [ALL], no root for the agent process).
ARG AGENT_EXTRA_PACKAGES=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git curl ca-certificates openssh-client build-essential \
        jq ripgrep httpie zip unzip sqlite3 dnsutils openssl less \
        nmap netcat-openbsd whois traceroute nikto \
        ${AGENT_EXTRA_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

# A couple of security/dev CLIs that are not Debian packages but install cleanly from PyPI: kept in their OWN venv, never the
# system Python and never agentd's own venv (/opt/agentd below), so neither gets a dependency conflict from the other's needs.
RUN python3 -m venv /opt/agent-tools \
    && /opt/agent-tools/bin/pip install --no-cache-dir sqlmap

# micromamba: one static binary, no root and no system package manager needed to use it. It lets the agent install almost
# anything from conda-forge (nmap already is apt-installed above, but e.g. go, rustc, newer nodejs, more exotic CLIs are not)
# into ITS OWN session folder at runtime — the agent decides what it needs, when it needs it; see ARCHITECTURE.md §26.
# The official bootstrap one-liner (documented by mamba-org for years, a stable URL unlike a project's shifting release-asset
# names): fetch the platform's tarball and extract just the `bin/micromamba` binary.
RUN set -eux; \
    case "$(uname -m)" in x86_64) MAMBA_ARCH=linux-64 ;; aarch64) MAMBA_ARCH=linux-aarch64 ;; *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;; esac; \
    curl -Ls "https://micro.mamba.pm/api/micromamba/${MAMBA_ARCH}/latest" | tar -xj -C /usr/local bin/micromamba; \
    /usr/local/bin/micromamba --version

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
COPY *.py ./

# `codex exec`'s commands run as `/bin/bash -lc "..."` (a LOGIN shell), and Debian's stock /etc/profile unconditionally
# OVERWRITES PATH for every login shell before it sources /etc/profile.d/*.sh — so the per-session tool locations (set as process
# env by agentd in spawn_env, for anything that is NOT a login shell) would otherwise be wiped out on every single command the
# agent runs. Appending them here, after that reset, is what makes self-installed tools (pip --user, npm -g, micromamba) actually
# reachable by bare name. $HOME is the session's own folder (set by spawn_env); these directories need not exist yet — a PATH
# entry is only resolved at the moment a command is looked up, so adding it ahead of time is harmless and future-proof.
RUN printf '%s\n' \
    'export MAMBA_ROOT_PREFIX="${MAMBA_ROOT_PREFIX:-$HOME/.micromamba}"' \
    'export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"' \
    'export PATH="$HOME/.local/bin:$NPM_CONFIG_PREFIX/bin:$MAMBA_ROOT_PREFIX/envs/tools/bin:/opt/agent-tools/bin:$PATH"' \
    > /etc/profile.d/agent-tools.sh

# The image's own `node` user (uid 1000) runs everything; the volume is its to write.
RUN mkdir -p /data/agent && chown node:node /data/agent
USER node
ENV AGENT_DATA_DIR=/data/agent
EXPOSE 8100
CMD ["/opt/agentd/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
