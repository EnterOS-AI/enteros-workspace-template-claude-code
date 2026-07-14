# Local development — Claude Code workspace template

These commands mirror the repository's current CI shape. Local unit tests do
not require a live workspace, control-plane token, or LLM credential.

## Prerequisites

- Python 3.11+
- Git
- Access to `git.moleculesai.app` and its package registry
- Docker only when reproducing the image build

## Clone and create an isolated environment

```bash
git clone https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-claude-code.git
cd molecule-ai-workspace-template-claude-code
git switch -c fix/describe-the-change

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install pytest pytest-asyncio pyyaml packaging
```

For checks that import the real private runtime, use the same canonical helper
as CI instead of adding a broad extra package index to every `pip` command:

```bash
rm -rf .molecule-ci-canonical
git clone --depth 1 https://git.moleculesai.app/molecule-ai/molecule-ci.git .molecule-ci-canonical
python3 .molecule-ci-canonical/scripts/install_workspace_dependencies.py --allow-missing
```

## Run the fast checks

```bash
python3 -m pytest tests/ -v
bash tests/test_entrypoint_restore.sh
bash tests/test_entrypoint_skills_link.sh
PROVIDERS_MANIFEST_FILE=internal/providers/providers.yaml \
  python3 .molecule-ci-canonical/scripts/validate-workspace-template.py --static-only
```

The tests under `tests/` intentionally stub runtime-only imports. The separate
`tests_conformance/` suite uses the real runtime and the pinned SDK and is run by
CI after those dependencies are installed.

## Build the image

The Dockerfile downloads `molecules-workspace-runtime` only from the private
Gitea Python index, then resolves public dependencies with the downloaded wheel
fixed in the solve. With package access and Docker available:

```bash
docker build -t workspace-template-claude-code:dev .
```

The container is not a standalone `python adapter.py` application. Its
entrypoint expects the platform's `/configs` and `/workspace` mounts and then
executes `molecule-runtime`. Use the repository tests for local adapter work and
the CI image/conformance jobs for the platform-shaped boot checks.

## Provider changes

Provider/model declarations live in `config.yaml`; runtime selection and auth
validation live in `adapter.py`. Preserve these rules when changing them:

1. A platform-injected `MOLECULE_RESOLVED_PROVIDER` wins.
2. An explicit endpoint override is preserved.
3. Credentials are read from environment variables but their values are never
   logged.
4. The provider projection checks must remain green.

Do not invent local config-overlay flags or hard-code a control-plane hostname;
neither is part of this adapter's command-line contract.

## Before opening a pull request

```bash
git diff --check
python3 -m pytest tests/ -q
```

Do not commit `.env` files, access tokens, generated credential files, or a
private package index credential.
