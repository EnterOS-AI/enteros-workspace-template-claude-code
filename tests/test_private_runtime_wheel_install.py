import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_RUNTIME_INDEX = (
    "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
)


def _dockerfile_instructions() -> list[str]:
    instructions: list[str] = []
    current = ""
    for raw_line in (ROOT / "Dockerfile").read_text().splitlines():
        stripped = raw_line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        continued = raw_line.rstrip().endswith("\\")
        piece = raw_line.rstrip()[:-1] if continued else raw_line.rstrip()
        current = f"{current} {piece.strip()}".strip()
        if not continued:
            instructions.append(current)
            current = ""
    if current:
        instructions.append(current)
    return instructions


def test_runtime_is_acquired_as_one_private_only_wheel() -> None:
    instructions = _dockerfile_instructions()

    assert f"ARG MOLECULE_RUNTIME_INDEX={PRIVATE_RUNTIME_INDEX}" in instructions

    downloads = [
        instruction
        for instruction in instructions
        if instruction.startswith("RUN ")
        and re.search(r"\bpip\s+download\b", instruction)
        and "molecules-workspace-runtime" in instruction
    ]
    assert len(downloads) == 1

    download = downloads[0]
    assert "pip download --isolated --only-binary=:all: --no-deps" in download
    assert '--index-url "$MOLECULE_RUNTIME_INDEX"' in download
    assert "--extra-index-url" not in download
    assert 'runtime_requirement="$(sed ' in download
    assert "requirements.txt" in download
    assert (
        'runtime_requirement="molecules-workspace-runtime==${RUNTIME_VERSION}"'
        in download
    )
    assert "set -- /tmp/molecule-runtime/*.whl" in download
    assert 'if [ "$#" -ne 1 ] || [ ! -f "$1" ]' in download


def test_runtime_wheel_and_public_requirements_share_one_isolated_solve() -> None:
    instructions = _dockerfile_instructions()
    installs = [
        instruction
        for instruction in instructions
        if instruction.startswith("RUN ")
        and re.search(r"\bpip\s+install\b", instruction)
    ]

    assert len(installs) == 1
    install = installs[0]
    assert "pip install --isolated" in install
    assert re.search(r"(?:^|\s)-r\s+requirements\.txt(?:\s|;|$)", install)
    assert "/tmp/molecule-runtime/*.whl" in install
    assert "--extra-index-url" not in install
