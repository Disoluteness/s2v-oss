"""V1.0 release freeze, carried forward by V1.1.

`1.0.0` is a promise, not a progress report: from here the compatibility rules in
``docs/API_STABILITY.md`` are in force. A promise that only lives in a document
is not enforced by anything — these tests are the enforcement. They pin three
kinds of thing:

* **the version**, in all three places it appears (the constant, the app factory's
  re-export, and the OpenAPI document a client actually reads),
* **the artefacts that carry the promise** (CHANGELOG, the stability policy, the
  release checklist, the examples), so deleting one to make a build pass is a
  failing test rather than a silent omission, and
* **the published catalogues** (error codes, webhook events, pipelines) — the
  vocabulary a client is told to branch on. Changing a count here is allowed, but
  it has to be a deliberate edit that comes with reading the policy first.

The same assertions are repeated against a running instance by gate section 25
``verify_release``. These pin the repository; that one pins the deployed image.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from apps.api.app.errors import ErrorCode, error_catalog_table
from apps.api.app.main import API_VERSION, app
from apps.api.app.permission_policy import OPEN_ENDPOINTS
from apps.api.app.version import APP_VERSION
from apps.api.app.webhooks import webhook_event_catalog
from packages.pipeline_kit.loader import PipelineLoader

ROOT = Path(__file__).resolve().parents[1]

#: The frozen contract version. Bumping this is the point of a major release, so
#: it is written here as a literal on purpose: a test that computed it from the
#: module it is testing would pass no matter what the module said.
FROZEN_VERSION = "1.1.0"


def _read(relative: str) -> str:
    path = ROOT / relative
    assert path.is_file(), f"{relative} is missing"
    return path.read_text(encoding="utf-8")


# --- the version ---------------------------------------------------------


def test_version_matches_the_frozen_constant() -> None:
    assert APP_VERSION == FROZEN_VERSION
    assert API_VERSION == APP_VERSION, "main must re-export the same constant"
    assert app.openapi()["info"]["version"] == APP_VERSION


def test_version_is_declared_in_exactly_one_place() -> None:
    """Two copies of a version constant drift; one cannot."""
    source = _read("apps/api/app/version.py")
    assert source.count("APP_VERSION = ") == 1
    assert FROZEN_VERSION in source


def test_version_string_does_not_leak_into_the_old_milestone() -> None:
    """The roadmap phrasing ("V8 closes at 0.9.x") must not survive the freeze."""
    assert "0.9.0" not in _read("apps/api/app/version.py")


# --- the artefacts carrying the promise ----------------------------------


def test_changelog_records_the_freeze_newest_first() -> None:
    text = _read("CHANGELOG.md")
    assert text.startswith("# Changelog")

    releases = [line for line in text.splitlines() if line.startswith("## [")]
    assert releases, "the changelog must list at least one release"
    # Keep-a-Changelog puts an in-progress section first; the newest *release*
    # must still precede every older one.
    dated = [line for line in releases if not line.startswith("## [Unreleased]")]
    assert dated, "the changelog must list at least one dated release"
    # Two axes share this file: the release version (1.0.x, what someone clones)
    # and the contract version (1.1.0, what a client talks to). A higher release
    # number can be older than the contract it ships, so ordering by number would
    # forbid the correct order. Dates are the one scale both axes share.
    days = re.findall(r"^## \[[^\]]+\] - (\d{4}-\d{2}-\d{2})$", text, flags=re.MULTILINE)
    assert len(days) >= 3, "the recent releases must carry dates"
    assert days == sorted(days, reverse=True), (
        "newest release first is the whole point of a changelog"
    )
    assert re.search(rf"^## \[{re.escape(FROZEN_VERSION)}\] - ", text, flags=re.MULTILINE), (
        "the frozen contract version must keep its own section"
    )
    unreleased = [line for line in releases if line.startswith("## [Unreleased]")]
    if unreleased:
        assert releases.index(unreleased[0]) < releases.index(dated[0]), (
            "Unreleased documents changes that are newer than the newest release"
        )


def test_stability_policy_is_published_and_referenced() -> None:
    policy = _read("docs/API_STABILITY.md")
    assert FROZEN_VERSION in policy
    assert "弃用" in policy
    # The policy is only meaningful if a reader can find what it classifies.
    for surface in ("/metrics", "/api/provider-callbacks"):
        assert surface in policy

    decisions = _read("docs/DECISIONS.md")
    assert "## ADR-006" in decisions
    assert "API_STABILITY.md" in decisions


def test_release_checklist_and_plan_exist() -> None:
    checklist = _read("docs/RELEASE_CHECKLIST.md")
    assert "回归" in checklist
    assert "回滚" in checklist
    assert _read("docs/V1.0_RELEASE_PLAN.md")


# --- the published catalogues -------------------------------------------


def test_error_catalogue_is_complete() -> None:
    catalog = error_catalog_table()
    assert len(ErrorCode) == 28
    assert len(catalog) == 28
    assert {entry["code"] for entry in catalog} == {code.value for code in ErrorCode}
    for entry in catalog:
        assert isinstance(entry["status_code"], int) and 400 <= entry["status_code"] < 600
        assert entry["category"]
        assert isinstance(entry["retryable"], bool)
        assert entry["description"], f"{entry['code']} has no description"


def test_maintenance_mode_is_documented_as_retryable() -> None:
    """The one V8.5 addition to the stable vocabulary, pinned where clients read it.

    503 plus ``retryable=true`` looks wrong until you remember what it describes:
    a maintenance window ends, so permanently abandoning the call would need a
    human restart.
    """
    by_code = {entry["code"]: entry for entry in error_catalog_table()}
    assert by_code["maintenance_mode"]["status_code"] == 503
    assert by_code["maintenance_mode"]["retryable"] is True


def test_webhook_event_vocabulary_is_frozen() -> None:
    catalog = webhook_event_catalog()
    assert len(catalog) == 6
    event_types = [entry["event_type"] for entry in catalog]
    assert len(set(event_types)) == len(event_types), "duplicate event names break subscribers"
    for entry in catalog:
        assert entry["description"], f"{entry['event_type']} has no description"
        assert entry["sources"], f"{entry['event_type']} maps to no internal sources"


def test_pipeline_catalogue_meets_the_release_bar() -> None:
    """Roadmap V1.0 asks for at least four stable pipelines.

    "Stable" is read as "no longer a draft" — the loader's status field is free
    text (draft / beta / production), and the bar is about which pipelines a
    release is willing to stand behind, not about how they were labelled on the
    way there.
    """
    pipelines = PipelineLoader().list_pipelines()
    assert len(pipelines) == 10

    keys = [pipeline.pipeline_key for pipeline in pipelines]
    assert len(set(keys)) == len(keys)
    assert "spokesperson_ad" in keys

    stable = [pipeline for pipeline in pipelines if pipeline.status != "draft"]
    assert len(stable) >= 4, sorted(p.pipeline_key for p in pipelines if p.status == "draft")
    for pipeline in pipelines:
        assert pipeline.stages, f"{pipeline.pipeline_key} declares no stages"
        assert pipeline.entrypoint_stage_key


def test_consumers_can_discover_every_catalogue_without_a_credential() -> None:
    """A catalogue nobody can read is not a published contract.

    These three are deliberately open (V7.1 / V7.3 / V1.0): they ship with the
    build, carry no tenant rows, and exist so an integrator does not have to read
    our source to discover the vocabulary.
    """
    for method, path in (
        ("GET", "/api/error-codes"),
        ("GET", "/api/webhook-events"),
        ("GET", "/api/pipelines"),
    ):
        assert (method, path) in OPEN_ENDPOINTS, f"{path} must stay discoverable"


# --- the examples --------------------------------------------------------


def test_curl_example_is_present_and_syntactically_sound() -> None:
    path = ROOT / "examples" / "curl" / "run_pipeline.sh"
    assert path.is_file(), "the curl example is the one path the SDKs do not cover"
    assert os.access(path, os.X_OK), "an example nobody can execute is a snippet"

    script = path.read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    # The exit-code contract is shared with the CLI and both SDK examples.
    assert "exit 3" in script

    result = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_examples_readme_maps_all_three_paths() -> None:
    text = _read("examples/README.md")
    for target in ("curl/run_pipeline.sh", "sdk/python/examples/", "sdk/typescript/examples/"):
        assert target in text, f"{target} is not reachable from the map"


def test_sdk_examples_still_parse() -> None:
    python_examples = sorted((ROOT / "sdk" / "python" / "examples").glob("*.py"))
    assert python_examples, "the Python examples are the canonical form, not a copy"
    for path in python_examples:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    script_examples = sorted((ROOT / "sdk" / "typescript" / "examples").glob("*.mjs"))
    assert script_examples
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the machine
        pytest.skip("node is not installed")
    for path in script_examples:
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_release_gate_checks_the_frozen_version() -> None:
    """The gate is the only thing that inspects a deployed image, so it must
    agree with the constant rather than carry its own copy of it. The gate is
    a standalone script and cannot import the app, so "agreeing" means it
    declares the same frozen literal *once*, as its own constant, and every
    check reads that constant — a second hardcoded literal would be a way for
    the two to drift.
    """
    gate = _read("scripts/verify_review_loop.py")
    assert gate.count(f'FROZEN_VERSION = "{FROZEN_VERSION}"') == 1
    assert gate.count('== FROZEN_VERSION') >= 3, "posture checks must read the constant"
    assert "0.9.0" not in gate


def test_deployment_docs_quote_the_current_version() -> None:
    """Documentation that quotes a version is documentation that goes stale.

    Kept as a test so the drift is caught at the same moment the constant moves.
    """
    deployment = _read("docs/DEPLOYMENT.md")
    assert FROZEN_VERSION in deployment
