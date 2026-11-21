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
#   network: nmap, netcat-openbsd (nc), whois, traceroute
# (nikto is NOT in bookworm's main repo — "Unable to locate package nikto" — so it is installed from its own repository below.)
# An admin can ADD more apt packages without touching this file — only AGENT_EXTRA_PACKAGES in .env, which goes straight into
# the same `apt-get install` below and needs a rebuild (`docker compose build agent`) to take effect, nothing lighter: the image
# is immutable at runtime (read-only root filesystem, cap_drop: [ALL], no root for the agent process).
# A larger red-team toolset for the panel's «Навыки» (subfinder/httpx/dnsx/nuclei, trufflehog, gitleaks, kubectl, impacket, awscli)
# is baked in further below, gated by AGENT_REDTEAM_TOOLS (default on).
ARG AGENT_EXTRA_PACKAGES=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git curl ca-certificates openssh-client build-essential \
        jq ripgrep httpie zip unzip sqlite3 dnsutils openssl less \
        nmap netcat-openbsd whois traceroute \
        perl libnet-ssleay-perl \
        ${AGENT_EXTRA_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

# nikto: a Perl script, run from a checkout of its own repository (perl and its TLS module come from apt above). An optional
# tool must not break the image: if the checkout or the self-test fails, the build goes on without it and says so.
RUN if git clone --depth 1 https://github.com/sullo/nikto /opt/nikto \
        && printf '#!/bin/sh\nexec perl /opt/nikto/program/nikto.pl "$@"\n' > /usr/local/bin/nikto \
        && chmod +x /usr/local/bin/nikto \
        && rm -rf /opt/nikto/.git \
        && nikto -Version >/dev/null; then \
        echo "nikto installed"; \
    else \
        rm -rf /opt/nikto /usr/local/bin/nikto; echo "WARNING: nikto was not installed (the agent can still get it itself at runtime)" >&2; \
    fi

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

# ── Baked red-team tooling for the «Навыки» (recon, web, secret-scanning, Active Directory, cloud, Kubernetes) ──────────────────
# The skills in the panel call these by bare name. The agent could fetch most itself at runtime, but then it re-downloads them every
# new session; baking them in makes them instant. Each tool is a pinned, published release — bump a version here and rebuild with
# `docker compose build agent`. All of it is BEST-EFFORT: a failed download skips that one tool with a warning and the build goes on
# (so a yanked release never bricks `./install.sh`; the agent can still self-install it — see docs/ARCHITECTURE.md §26). Turn the whole
# block off with AGENT_REDTEAM_TOOLS=0 in .env. NOTE: these are for AUTHORIZED testing only; the container stays a locked sandbox
# (no root, read-only FS, cap_drop ALL), so tools that need root, apt or raw sockets (Responder, hashcat) belong on «Агент», not here.
ARG AGENT_REDTEAM_TOOLS=1
# ProjectDiscovery recon chain — one static Go binary each, from the project's own release zip.
RUN if [ "${AGENT_REDTEAM_TOOLS}" = "1" ]; then \
    case "$(uname -m)" in x86_64) A=amd64 ;; aarch64) A=arm64 ;; *) A="" ;; esac; \
    if [ -n "$A" ]; then for t in "subfinder:2.6.6" "httpx:1.6.9" "dnsx:1.2.1" "nuclei:3.3.7"; do \
      n="${t%%:*}"; v="${t##*:}"; \
      if curl -fsSL "https://github.com/projectdiscovery/$n/releases/download/v$v/${n}_${v}_linux_${A}.zip" -o "/tmp/$n.zip" \
         && unzip -qo "/tmp/$n.zip" -d "/tmp/$n" && install -m755 "/tmp/$n/$n" "/usr/local/bin/$n"; then echo "baked $n $v"; \
      else echo "WARNING: $n not baked (the agent can get it itself)" >&2; fi; rm -rf "/tmp/$n" "/tmp/$n.zip"; \
    done; fi; fi
# trufflehog & gitleaks (secret scanning) and kubectl — single binaries.
RUN if [ "${AGENT_REDTEAM_TOOLS}" = "1" ]; then \
    case "$(uname -m)" in x86_64) A=amd64; GL=x64; K=amd64 ;; aarch64) A=arm64; GL=arm64; K=arm64 ;; *) A="" ;; esac; \
    if [ -n "$A" ]; then \
      curl -fsSL "https://github.com/trufflesecurity/trufflehog/releases/download/v3.88.0/trufflehog_3.88.0_linux_${A}.tar.gz" | tar -xz -C /usr/local/bin trufflehog || echo "WARNING: trufflehog not baked" >&2; \
      curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_${GL}.tar.gz" | tar -xz -C /usr/local/bin gitleaks || echo "WARNING: gitleaks not baked" >&2; \
      { curl -fsSL "https://dl.k8s.io/release/v1.37.2/bin/linux/${K}/kubectl" -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl && echo "baked kubectl"; } || echo "WARNING: kubectl not baked" >&2; \
    fi; fi
# impacket (AD: secretsdump, ntlmrelayx, GetUserSPNs, wmiexec…) and awscli — into the same venv as sqlmap. impacket's example scripts
# install as `secretsdump.py`; the skills call `secretsdump`, `ntlmrelayx` and `impacket-ntlmrelayx` too, so expose all three forms.
RUN if [ "${AGENT_REDTEAM_TOOLS}" = "1" ]; then \
      if /opt/agent-tools/bin/pip install --no-cache-dir "impacket==0.13.1" "awscli==1.46.3"; then \
        for f in /opt/agent-tools/bin/*.py; do b="$(basename "$f" .py)"; ln -sf "$f" "/usr/local/bin/$b"; ln -sf "$f" "/usr/local/bin/impacket-$b"; done; \
        ln -sf /opt/agent-tools/bin/aws /usr/local/bin/aws; echo "baked impacket + awscli"; \
      else echo "WARNING: impacket/awscli not baked (the agent can pip-install them)" >&2; fi; fi

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
