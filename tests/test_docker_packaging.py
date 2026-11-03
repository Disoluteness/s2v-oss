from pathlib import Path

# Everything the Dockerfile COPYs into the runtime image. If any of these ends
# up ignored by .dockerignore the build silently ships an incomplete app.
REQUIRED_IN_BUILD_CONTEXT = (
    "apps",
    "packages",
    "pipelines",
    "tool_catalog",
    "infra",
    "scripts",
    "pyproject.toml",
    "README.md",
    "alembic.ini",
)


def test_api_dockerfile_copies_runtime_catalogs() -> None:
    dockerfile = Path("infra/docker/api.Dockerfile").read_text(encoding="utf-8")

    assert "COPY pipelines ./pipelines" in dockerfile
    assert "COPY tool_catalog ./tool_catalog" in dockerfile
    assert "COPY infra ./infra" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile


def _dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerignore_keeps_everything_the_image_needs() -> None:
    patterns = set(_dockerignore_patterns())

    for entry in REQUIRED_IN_BUILD_CONTEXT:
        assert entry not in patterns, f".dockerignore would strip {entry} from the image"

    # The heavy directories are the whole point of having this file.
    assert ".venv/" in patterns
    assert ".pytest_tmp/" in patterns
    assert "__pycache__/" in patterns


def test_local_secrets_are_not_baked_into_the_image() -> None:
    """``.env`` reaches the containers through compose, never through the image."""
    dockerfile = Path("infra/docker/api.Dockerfile").read_text(encoding="utf-8")

    assert "COPY . " not in dockerfile
    assert "\nCOPY .env" not in dockerfile
    assert ".env" in set(_dockerignore_patterns())
