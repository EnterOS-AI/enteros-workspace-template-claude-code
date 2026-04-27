FROM python:3.11-slim

# System deps — curl/gosu/node/npm for the runtime; git + gh for agent
# autonomy (agents run `gh issue list`, `gh issue create`, `gh issue edit
# --add-assignee`, `git clone`, etc. per their idle/cron prompts).
# Without these the team's claim-and-ship loop silently returns
# "(no response generated)" because tools error out.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gosu nodejs npm ca-certificates git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install claude-code CLI via npm
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || true

# Create agent user
RUN useradd -u 1000 -m -s /bin/bash agent
WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy adapter code
COPY adapter.py .
COPY __init__.py .
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

# Drop-priv entrypoint — claude-code refuses --dangerously-skip-permissions
# as root, so we run molecule-runtime as the agent user (uid 1000).
# The script handles volume-ownership fix + session-dir symlink before
# exec'ing via gosu.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
