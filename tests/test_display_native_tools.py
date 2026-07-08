from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_bakes_native_display_control_tools():
    dockerfile = (ROOT / "Dockerfile").read_text()

    for package in ("xdotool", "scrot"):
        assert package in dockerfile


def test_entrypoint_prepares_agent_downloads_dir():
    entrypoint = (ROOT / "entrypoint.sh").read_text()

    assert "mkdir -p /home/agent/Downloads" in entrypoint
    assert "chown agent:agent /home/agent/Downloads" in entrypoint
