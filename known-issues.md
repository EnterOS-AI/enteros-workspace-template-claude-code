# Support notes — Claude Code workspace template

The live issue tracker is the source of truth for open defects:

<https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-template-claude-code/issues>

This file records current support boundaries only. Old boot, heartbeat,
schema-guessing, and image-host workarounds were removed because they no longer
describe the code on `main`.

## Supported boot path

The image boots through `entrypoint.sh` and then `molecule-runtime`. Running
`adapter.py` directly, supplying an invented config-overlay flag, or polling a
task endpoint from the adapter is not a supported development or production
path.

## Authentication failures

At setup, `adapter.py` resolves the selected provider and checks the accepted
credential variables for that route. If none is available, setup fails with the
accepted variable names; secret values are not logged. Check the selected model,
resolved provider, and secret binding together rather than assuming every model
uses `CLAUDE_CODE_OAUTH_TOKEN`.

## Runtime and image validation

The private runtime wheel and the adapter conformance suite are validated in CI.
The unit suite under `tests/` uses controlled stubs and is not evidence that a
locally launched container has the platform mounts, identity, or credentials it
needs. Use the CI image and conformance jobs for that proof.

## Optional source mirror credentials

GitHub mirror credentials are opt-in and are not on the boot-critical path.
`tests/test_no_github_critical_path.py` guards that contract. Do not enable the
mirror helpers as a workaround for Gitea repository or package access.
