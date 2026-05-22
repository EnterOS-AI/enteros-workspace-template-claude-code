from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_does_not_use_github_go_setup():
    workflow = (ROOT / ".gitea" / "workflows" / "ci.yml").read_text()

    assert "actions/setup-go" not in workflow
    assert "go run ./cmd/t4-contract-dump" not in workflow
    assert "t4_capabilities.yaml" in workflow
    assert "git.moleculesai.app" in workflow


def test_dockerfile_does_not_install_github_cli_from_github():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "cli.github.com" not in dockerfile
    assert "githubcli-archive-keyring" not in dockerfile
    assert "apt-get install -y --no-install-recommends gh" not in dockerfile


def test_github_mirror_credentials_are_opt_in():
    entrypoint = (ROOT / "entrypoint.sh").read_text()

    assert 'ENABLE_GITHUB_MIRROR_CREDENTIALS:-false' in entrypoint
    assert "gh auth login" not in entrypoint
