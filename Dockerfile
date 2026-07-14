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
#
# rsync (legacy restore compatibility, cp#326):
#   when a restored secondary device is present at /dev/xvdb,
#   entrypoint.sh copies /configs, /workspace, and /home/agent/.claude
#   into the container root on first boot. Without rsync that optional
#   compatibility path would silently no-op.
#
# e2fsprogs (added cp#326 2026-05-26):
#   provides /sbin/blkid + /sbin/e2label so the restore code can
#   probe the secondary volume's filesystem type before mounting.
#   Already pulled in indirectly by util-linux on Debian-slim but
#   pinning it explicit makes the dependency self-documenting and
#   survives a future base-image change.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gosu nodejs npm ca-certificates git sudo util-linux docker.io xdotool scrot \
    rsync e2fsprogs \
    && rm -rf /var/lib/apt/lists/*

# Install claude-code CLI via npm — fail-closed so an npm outage, package
# rename, auth failure, or Node/npm breakage fails the image build instead
# of producing a green image without the primary runtime engine (#75).
RUN npm install -g @anthropic-ai/claude-code
# Verify the CLI resolved in PATH (catches masked install failures,
# package renames, or npm prefix misconfig).
RUN command -v claude >/dev/null 2>&1 || (echo "ERROR: claude CLI not found in PATH" >&2 && exit 1)

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
# exact runtime version the private registry just published. Including it
# as an ARG
# changes the cache key for the pip install layer below — without
# this, identical Dockerfile + identical requirements.txt content
# would let docker reuse the cached layer with the previous version
# baked in (the cache trap that bit us 5x on 2026-04-27).
# Empty default = falls back to whatever requirements.txt resolves to.
ARG RUNTIME_VERSION=
ARG MOLECULE_RUNTIME_INDEX=https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/

# Parse and remove the runtime requirement before the public solve so a direct
# reference can never bypass private acquisition. Public requirements and
# runtime transitive dependencies are then resolved together from pip's
# default public source; the local wheel fixes the runtime candidate for that
# single solve. --isolated keeps ambient pip configuration and index
# environment variables out of both operations.
COPY requirements.txt .
COPY scripts/prepare_runtime_requirements.py /tmp/prepare_runtime_requirements.py
RUN set -eu; \
    runtime_project="molecules-workspace-runtime"; \
    rm -rf /tmp/molecule-runtime; \
    rm -f /tmp/template-requirements.txt; \
    mkdir -p /tmp/molecule-runtime; \
    runtime_requirement="$(python3 /tmp/prepare_runtime_requirements.py \
      requirements.txt /tmp/template-requirements.txt \
      --runtime-version "${RUNTIME_VERSION}")"; \
    if [ "${runtime_requirement#${runtime_project}}" = "${runtime_requirement}" ]; then \
      echo "ERROR: runtime requirement was not canonicalized" >&2; \
      exit 1; \
    fi; \
    pip download --isolated --only-binary=:all: --no-deps \
      --index-url "$MOLECULE_RUNTIME_INDEX" \
      --dest /tmp/molecule-runtime "${runtime_requirement}"; \
    set -- /tmp/molecule-runtime/*.whl; \
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then \
      echo "ERROR: private runtime acquisition did not produce exactly one wheel" >&2; \
      exit 1; \
    fi; \
    pip install --isolated --no-cache-dir /tmp/molecule-runtime/*.whl \
      -r /tmp/template-requirements.txt; \
    rm -rf /tmp/molecule-runtime /tmp/template-requirements.txt

# --- Pre-bake the management-MCP server (base-runtime helper; task #54) ---
# The kind=platform concierge launches `npx --prefer-offline @molecule-ai/mcp-server@<PIN>`
# in a HARD-deadline enumeration spawn at boot; without a warm cache it cold-pulls
# -> ETARGET / CF-WAF throttle -> #1027 fail-close (launch-side of RCA #2970). The bake
# LOGIC + the pinned version now live ONCE in the base runtime (molecule_runtime, pinned
# to the SDK contract management_mcp_server block) — this template DELEGATES to the shared
# helper instead of carrying its own bake + ARG (ADR-004: SDK contract -> base-runtime
# default -> per-adapter override-if-needed; no per-template fork). Replaces the former
# per-template bake that had drifted to a STALE 1.8.1 pin (the plugin fragment pins 1.8.2)
# — the SSOT delegation always bakes the contract pin. claude-code ships node globally on
# PATH, so no MOLECULE_PREBAKE_NODE_BIN override. The helper's build-time OFFLINE
# self-check fails the image if the bake is broken.
USER agent
RUN bash "$(python3 -c 'import molecule_runtime, os; print(os.path.dirname(molecule_runtime.__file__))')/scripts/prebake-mgmt-mcp.sh"
USER root


# MOLECULE-HOTFIX (claude-code 2.1.150 / agent-sdk 0.2.84): apply in-place
# SDK patch so the receive_messages loop treats is_error+subtype=success as
# end-of-stream rather than raising Exception("... success"). See
# scripts/patch_claude_sdk_2_1_150.py for the rationale + upstream removal
# criteria. Idempotent; fails the build if the upstream SDK has been
# updated so we notice the workaround is stale.
COPY scripts/patch_claude_sdk_2_1_150.py /tmp/patch_claude_sdk_2_1_150.py
RUN python3 /tmp/patch_claude_sdk_2_1_150.py && rm /tmp/patch_claude_sdk_2_1_150.py

# Copy adapter code
COPY adapter.py .
COPY __init__.py .
# Provider registry. The adapter's _load_providers walks 4 paths:
#   1. /opt/adapter/config.yaml          — legacy/self-managed compatibility
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

# Gitea credential-safety wrapper (#34).
#   - setup-gitea-netrc.sh writes ~/.netrc from the platform-projected
#     GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD env vars, mode 600, atomically.
#   - gitea-curl is a structural argv-scan wrapper that forces curl to read
#     credentials from ~/.netrc and rejects any inline -u/--user or
#     Authorization header, keeping tokens out of process argv / activity logs.
# Vendored from molecule-ci; the same pattern should propagate to the other
# runtime templates (codex / hermes / openclaw) as a follow-up.
COPY scripts/setup-gitea-netrc.sh /usr/local/bin/setup-gitea-netrc.sh
COPY bin/gitea-curl /usr/local/bin/gitea-curl
RUN chmod +x /usr/local/bin/setup-gitea-netrc.sh /usr/local/bin/gitea-curl

# Drop-priv entrypoint — claude-code refuses --dangerously-skip-permissions
# as root, so we run molecule-runtime as the agent user (uid 1000).
# The script handles volume-ownership fix + session-dir symlink before
# exec'ing via gosu.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
