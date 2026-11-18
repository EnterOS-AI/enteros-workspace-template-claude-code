from pathlib import Path

import pytest

from scripts.prepare_runtime_requirements import (
    RuntimeRequirementError,
    prepare_runtime_requirements,
)


def _prepare(
    tmp_path: Path,
    content: str,
    *,
    runtime_version: str = "",
) -> tuple[str, str]:
    source = tmp_path / "requirements.txt"
    filtered = tmp_path / "requirements-public.txt"
    source.write_text(content)
    requirement = prepare_runtime_requirements(
        source,
        filtered,
        runtime_version=runtime_version,
    )
    return requirement, filtered.read_text()


def test_reconstructs_canonical_runtime_specifier_and_filters_source(
    tmp_path: Path,
) -> None:
    requirement, filtered = _prepare(
        tmp_path,
        "# private runtime\n"
        "Molecules_Workspace_Runtime>=0.3.11,<0.4\n"
        "python-multipart>=0.0.27\n",
    )

    assert requirement == "molecules-workspace-runtime<0.4,>=0.3.11"
    assert "Molecules_Workspace_Runtime" not in filtered
    assert filtered == "# private runtime\npython-multipart>=0.0.27\n"


def test_exact_runtime_version_overrides_requirements_specifier(
    tmp_path: Path,
) -> None:
    requirement, filtered = _prepare(
        tmp_path,
        "molecules-workspace-runtime>=0.3.11,<0.4\nclaude-agent-sdk>=0.1.58\n",
        runtime_version="0.3.125",
    )

    assert requirement == "molecules-workspace-runtime==0.3.125"
    assert filtered == "claude-agent-sdk>=0.1.58\n"


def test_rejects_runtime_direct_reference(tmp_path: Path) -> None:
    with pytest.raises(RuntimeRequirementError, match="direct URL"):
        _prepare(
            tmp_path,
            "molecules-workspace-runtime @ "
            "https://example.invalid/molecules_workspace_runtime-9.9.9.whl\n",
        )


@pytest.mark.parametrize(
    "runtime_line",
    (
        "molecules-workspace-runtime[unsafe]>=0.3.11",
        'molecules-workspace-runtime>=0.3.11; python_version >= "3.11"',
    ),
)
def test_rejects_runtime_extras_and_markers(
    tmp_path: Path,
    runtime_line: str,
) -> None:
    with pytest.raises(RuntimeRequirementError, match="extras or markers"):
        _prepare(tmp_path, f"{runtime_line}\n")


def test_rejects_duplicate_runtime_candidates(tmp_path: Path) -> None:
    with pytest.raises(RuntimeRequirementError, match="exactly once"):
        _prepare(
            tmp_path,
            "molecules-workspace-runtime>=0.3.11\nmolecules_workspace_runtime<0.4\n",
        )


def test_rejects_nested_requirements_that_can_reintroduce_runtime(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeRequirementError, match="pip directives"):
        _prepare(
            tmp_path,
            "molecules-workspace-runtime>=0.3.11\n-r nested.txt\n",
        )


def test_rejects_backslash_continuations(tmp_path: Path) -> None:
    with pytest.raises(RuntimeRequirementError, match="backslash continuations"):
        _prepare(
            tmp_path,
            "molecules-workspace-\\\nruntime>=0.3.11\n",
        )
