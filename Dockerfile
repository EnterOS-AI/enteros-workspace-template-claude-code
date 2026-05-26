FROM python:3.11-slim

# System deps — curl/gosu/node/npm for the runtime; git for agent
# autonomy against the Molecule-owned Gitea middleman.
# Without these the team's claim-and-ship loop silently returns
# "(no response generated)" because tools error out.
#
# T4 escalation leg (RFC internal#456 §9 / PR#474):
#   sudo + util-linux(nsenter) + docker.io(CLI) are baked here so the
#   uid-1000 `agent` (see useradd below — UNCHANGED, agent stays
#   uid-1000) has a wired, audited path to host root inside the
#   provisioner's `--privileged --pid=host -v /:/host
#   -v /var/run/docker.sock:/var/run/docker.sock` container. Without
#   sudo, a uid-1000 process in --privileged CANNOT nsenter/chroot
#   /host (--privileged grants caps to root, not uid-1000) and cannot
#   use the root:docker 0660 docker.sock — T4 would be
#   provisioner-shape-only (the documented ABSENT-escalation-leg gap).
#   The sudoers drop-in + docker-group add are below, after useradd,
#   so `agent` exists. This is ADDITIVE: it does NOT change the agent
#   uid and does NOT change /configs token ownership (still uid-1000,
#   enforced by entrypoint.sh + the Layer-3 conformance gate).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gosu nodejs npm ca-certificates git sudo util-linux docker.io xdotool scrot \
    && rm -rf /var/lib/apt/lists/*

# Install claude-code CLI via npm
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || true

# Create agent user — UNCHANGED. The agent runs as uid-1000; the T4
# escalation leg below is additive and does NOT promote the agent to
# root. claude-code still refuses --dangerously-skip-permissions as
# root, and /configs/.auth_token must stay agent-owned (Hermes
# list_peers 401 class — RFC internal#456 §10).
RUN useradd -u 1000 -m -s /bin/bash agent && \
    mkdir -p /agent-home && \
    chown agent:agent /agent-home

# --- T4 escalation leg (RFC internal#456 §9.3 / PR#474) ---
# Wired path: uid-1000 agent -> host root inside the provisioner's
# --privileged --pid=host -v /:/host -v docker.sock container.
#   1. NOPASSWD sudoers drop-in (mode 0440, visudo-validated at build
#      so a malformed sudoers can never ship a broken-sudo image).
#   2. agent in the `docker` group so the bind-mounted root:docker
#      0660 /var/run/docker.sock is usable without sudo.
# Atomic co-sequencing (RFC §10): this ships in the SAME image
# revision as the uid-1000 + agent-owned-token entrypoint contract;
# the Layer-3 conformance gate asserts BOTH on the running container.
RUN set -eux; \
    printf 'agent ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/agent-t4; \
    chmod 0440 /etc/sudoers.d/agent-t4; \
    visudo -cf /etc/sudoers.d/agent-t4; \
    groupadd -f docker; \
    groupadd -g 988 -f docker-host || true; \
    usermod -aG docker agent; \
    usermod -aG docker-host agent || true; \
    id agent

WORKDIR /app

# RUNTIME_VERSION is forwarded from the reusable publish workflow as
# a docker build-arg. When set (cascade-triggered builds), it's the
# exact runtime version PyPI just published. Including it as an ARG
# changes the cache key for the pip install layer below — without
# this, identical Dockerfile + identical requirements.txt content
# would let docker reuse the cached layer with the previous version
# baked in (the cache trap that bit us 5x on 2026-04-27).
# Empty default = falls back to whatever requirements.txt resolves to.
ARG RUNTIME_VERSION=
ARG PIP_INDEX_URL=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/

# Install Python deps. The RUNTIME_VERSION ARG is a no-op argument to
# the RUN command itself but its presence as a declared ARG above
# means buildx hashes it into the cache key.
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt && \
    if [ -n "${RUNTIME_VERSION}" ]; then \
      pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \
    fi

# Copy adapter code
COPY adapter.py .
COPY __init__.py .
# Provider registry. The adapter's _load_providers walks 4 paths:
#   1. /opt/adapter/config.yaml          — provisioner-managed canonical
#   2. os.path.dirname(__file__)/config.yaml  — alongside adapter.py (this image)
#   3. ${WORKSPACE_CONFIG_PATH}/config.yaml   — workspace per-instance overrides
#   4. _BUILTIN_PROVIDERS                — oauth + anthropic-api only
# On this image /opt/adapter/ is never populated by the platform
# provisioner, so path 2 (/app/config.yaml) is the load-bearing one.
# Without this COPY the file isn't in the image, all 3 file paths fail,
# and _load_providers falls through to _BUILTIN_PROVIDERS — every
# MiniMax/GLM/Kimi/DeepSeek model silently routes to anthropic-oauth →
# "Not logged in. Please run /login" at first LLM call. Caused the
# canary's 38h chronic red on 2026-05-07/08 (molecule-core#129).
COPY config.yaml .
# Adapter-specific executor — owned by THIS template (universal-runtime
# refactor, molecule-core task #87). Lives alongside adapter.py so
# Python's import system picks the local /app/claude_sdk_executor.py
# before the same-named module that older molecule-runtime versions
# also shipped under site-packages. Once molecule-core drops the file
# from its workspace/ package and bumps the runtime PyPI version, the
# template will be the sole source of truth.
COPY claude_sdk_executor.py .

# Set the adapter module for runtime discovery
ENV ADAPTER_MODULE=adapter

# Optional GitHub mirror credential helper + background refresh daemon.
# GitHub is not on the critical path; these scripts are inert unless
# ENABLE_GITHUB_MIRROR_CREDENTIALS=true is set by an operator.
COPY scripts/molecule-git-token-helper.sh /app/scripts/molecule-git-token-helper.sh
COPY scripts/molecule-gh-token-refresh.sh /app/scripts/molecule-gh-token-refresh.sh
RUN chmod +x /app/scripts/molecule-git-token-helper.sh /app/scripts/molecule-gh-token-refresh.sh

# Generic GIT_ASKPASS helper — image-side companion to molecule-core PR
# #1525 (workspace-server applyAgentGitIdentity, merge_sha 73a09443a086).
# Reads HTTPS Basic-Auth credentials from env vars (GIT_HTTP_USERNAME /
# GIT_HTTP_PASSWORD, with GITEA_USER / GITEA_TOKEN as fallback) and emits
# them on the git credential-prompt protocol, so container-side `git` can
# authenticate to any private HTTPS remote without on-disk ~/.gitconfig
# or ~/.git-credentials mutation. The platform provisioner sets
# GIT_ASKPASS=/usr/local/bin/molecule-askpass via applyAgentGitIdentity;
# until this binary ships in the runtime image, git invocations error
# with "exec: /usr/local/bin/molecule-askpass: not found" (forward-only
# pin gap — same class as Hermes list_peers and codex template breakage,
# fixed image-side here).
#
# No hardcoded hostnames or vendor names — the script body is identical
# to the one shipped in molecule-core workspace/scripts/molecule-askpass
# and the parallel external workspace template repos, so any deployer
# can fork this template and use it against their own git host without
# editing.
COPY scripts/molecule-askpass /usr/local/bin/molecule-askpass
RUN chmod +x /usr/local/bin/molecule-askpass

# Drop-priv entrypoint — claude-code refuses --dangerously-skip-permissions
# as root, so we run molecule-runtime as the agent user (uid 1000).
# The script handles volume-ownership fix + session-dir symlink before
# exec'ing via gosu.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
