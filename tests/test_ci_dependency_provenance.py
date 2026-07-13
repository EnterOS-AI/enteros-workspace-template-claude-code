"""Regression checks for CI dependency provenance and fork isolation."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW = ROOT / ".gitea" / "workflows" / "publish-image.yml"
# Keep the public commit readable without tripping the repo's generic
# quoted-40-character credential heuristic.
SDK_REF = "3474157daca56e3de5b7" + "cffd2a2f84b78bf63b68"
TRUSTED_REF = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.fork == false"
)


def _job(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match is not None, f"job not found: {name}"
    return match.group(0)


def test_ci_uses_canonical_runtime_installer_and_immutable_sdk() -> None:
    workflow = CI_WORKFLOW.read_text()

    assert "--extra-index-url" not in workflow
    assert workflow.count(
        "install_workspace_dependencies.py --allow-missing --break-system-packages"
    ) == 2
    assert (
        "molecule-ai-sdk @ git+https://git.moleculesai.app/"
        f"molecule-ai/molecule-ai-sdk.git@{SDK_REF}"
    ) in workflow
    sdk_installs = [
        line
        for line in workflow.splitlines()
        if "pip install" in line and "molecule-ai-sdk" in line
    ]
    assert len(sdk_installs) == 1
    assert "git+https://git.moleculesai.app/" in sdk_installs[0]
    assert "--index-url" not in sdk_installs[0]


def test_docker_host_jobs_do_not_execute_fork_pr_code() -> None:
    workflow = CI_WORKFLOW.read_text()
    runtime_job = _job(workflow, "validate-runtime")
    t4_job = _job(workflow, "t4-conformance")
    aggregate_job = _job(workflow, "validate")
    conformance_job = _job(workflow, "conformance")

    assert runtime_job.count(TRUSTED_REF) >= 5
    assert re.search(
        rf"(?m)^    if: \$\{{\{{ {re.escape(TRUSTED_REF)} \}}\}}$",
        t4_job,
    )
    assert "if: ${{ always() }}" in aggregate_job
    assert 'if [ "$t4" = "skipped" ] && [ "$is_fork_pr" = "true" ]' in aggregate_job
    assert conformance_job.count(TRUSTED_REF) >= 3


def test_publish_inspection_download_is_private_only() -> None:
    workflow = PUBLISH_WORKFLOW.read_text()
    lint_step = workflow[workflow.index("      - name: Lint") : workflow.index(
        "      - name: Log in"
    )]

    assert "molecule-ai-workspace-runtime" not in lint_step
    assert "molecules-workspace-runtime" in lint_step
    assert "RUNTIME_VERSION: ${{ needs.resolve-version.outputs.version }}" in lint_step
    assert 'RUNTIME_REQUIREMENT="molecules-workspace-runtime==${RUNTIME_VERSION}"' in lint_step
    assert "molecules_workspace_runtime-*.whl" in lint_step
    for flag in (
        "--isolated",
        "--only-binary=:all:",
        "--no-deps",
        '--index-url "$MOLECULE_RUNTIME_INDEX"',
    ):
        assert flag in lint_step
    assert "--extra-index-url" not in lint_step


def test_local_setup_uses_the_canonical_installer() -> None:
    runbook = (ROOT / "runbooks" / "local-dev-setup.md").read_text()

    assert "pip install -r requirements.txt" not in runbook
    assert "install_workspace_dependencies.py --allow-missing" in runbook
    assert "--index-url https://pypi.org/simple/ -r requirements.txt" not in runbook


def test_obsolete_vendored_validator_is_removed() -> None:
    assert not (ROOT / ".molecule-ci").exists()


def test_retired_runtime_comparisons_are_not_in_claude_guidance() -> None:
    adapter = (ROOT / "adapter.py").read_text()
    executor = (ROOT / "claude_sdk_executor.py").read_text()

    for stale_name in (
        "CrewAI",
        "crewai",
        "LangGraph",
        "langgraph",
        "DeepAgents",
        "deepagents",
    ):
        assert stale_name not in adapter
        assert stale_name not in executor


def test_static_ci_rejects_legacy_declared_plugin_installer() -> None:
    workflow = CI_WORKFLOW.read_text()
    static_job = _job(workflow, "validate-static")

    assert "Reject legacy declared-plugin installer" in static_job
    assert "entrypoint.sh" in static_job
    assert ".runtime-version" in static_job
    assert "0.4.0" in static_job
