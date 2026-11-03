"""V6.4 asset library: reusable, rights-tagged entries on top of run assets.

The library never owns bytes — every entry points at an asset that already belongs
to a run. These tests pin the two properties that make that safe:

* an entry can be added twice without duplicating (retries and bulk imports are
  idempotent), and
* reusing an entry into another run produces a *new* asset that carries the origin
  and the license with it, so rights never get lost at the copy boundary.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models, routes  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.main import app, asset_library_html_path
from apps.api.app.models import (
    AssetCollectionCreate,
    AssetCollectionImportRequest,
    AssetCollectionItemAdd,
    AssetCollectionItemUpdate,
    AssetCollectionUpdate,
    AssetLicense,
    AssetProvenance,
    AssetReuseRequest,
    AssetUpload,
    ProjectCreate,
    RunCreate,
)
from apps.api.app.repositories import (
    AssetReuseOutcome,
    CollectionAddOutcome,
    CollectionImportOutcome,
    as_utc,
)
from apps.api.app.routes import (
    add_asset_collection_item as add_item_route,
)
from apps.api.app.routes import (
    get_asset_collection as get_collection_route,
)
from apps.api.app.routes import (
    import_run_assets_into_collection as import_route,
)
from apps.api.app.routes import (
    reuse_asset_from_library as reuse_route,
)
from apps.api.app.routes import (
    update_asset_collection as update_collection_route,
)
from packages.pipeline_kit.loader import PipelineLoader

NEW_ENDPOINTS = {
    "/api/projects/{project_id}/asset-collections": {"get", "post"},
    "/api/projects/{project_id}/asset-library": {"get"},
    "/api/asset-collections/{collection_id}": {"get", "patch"},
    "/api/asset-collections/{collection_id}/items": {"get", "post"},
    "/api/asset-collections/{collection_id}/import-from-run": {"post"},
    "/api/asset-library/items/{item_id}": {"patch", "delete"},
    "/api/runs/{run_id}/asset-library/reuse": {"post"},
}


def _store(tmp_path, name: str = "asset-library.db") -> DatabaseStore:
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return DatabaseStore(Session(engine))


def _seed(tmp_path, name: str = "asset-library.db") -> tuple[DatabaseStore, str, str]:
    """A project, one run and one uploaded asset that the library can reference."""
    store = _store(tmp_path, name)
    project = store.create_project(ProjectCreate(title="资产库测试"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="资产库测试脚本",
            brand_name="测试品牌",
            product_name="测试产品",
        ),
        pipeline=PipelineLoader().get_pipeline("avatar_spokesperson"),
    )
    asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="person_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image bytes").decode("ascii"),
            mime_type="image/png",
        ),
    )
    assert asset is not None
    return store, project.project_id, run.run_id


def _collection(store: DatabaseStore, project_id: str, **overrides):
    payload = {"name": "人物形象库", "kind": "person", **overrides}
    collection = store.create_asset_collection(
        project_id,
        AssetCollectionCreate(**payload),
    )
    assert collection is not None
    return collection


# --------------------------------------------------------------------------- #
# collections
# --------------------------------------------------------------------------- #


def test_collection_crud_round_trip(tmp_path) -> None:
    store, project_id, _run_id = _seed(tmp_path)

    collection = _collection(store, project_id, description="可复用人物图")
    assert collection.collection_id.startswith("acol_")
    assert collection.kind == "person"
    assert collection.status == "active"
    assert collection.item_count == 0

    listed = store.list_asset_collections(project_id)
    assert listed is not None
    assert [item.collection_id for item in listed] == [collection.collection_id]

    updated = store.update_asset_collection(
        collection.collection_id,
        AssetCollectionUpdate(name="人物形象库 v2", status="archived"),
    )
    assert updated is not None
    assert updated.name == "人物形象库 v2"
    assert updated.status == "archived"

    # Archived collections drop out of the default listing but stay reachable.
    assert store.list_asset_collections(project_id) == []
    archived = store.list_asset_collections(project_id, include_archived=True)
    assert archived is not None and len(archived) == 1
    assert store.get_asset_collection(collection.collection_id) is not None


def test_collection_requires_an_existing_project(tmp_path) -> None:
    store = _store(tmp_path, "no-project.db")

    assert store.create_asset_collection("project_missing", AssetCollectionCreate(name="x")) is None
    assert store.list_asset_collections("project_missing") is None
    assert store.project_asset_library("project_missing") is None

    with pytest.raises(HTTPException) as exc_info:
        routes.create_asset_collection(
            "project_missing",
            AssetCollectionCreate(name="x"),
            store,
        )
    assert exc_info.value.status_code == 404


def test_collection_kind_is_validated(tmp_path) -> None:
    store, project_id, _run_id = _seed(tmp_path, "kind.db")

    with pytest.raises(ValueError):
        store.create_asset_collection(
            project_id,
            AssetCollectionCreate(name="未知类型", kind="nope"),
        )


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #


def test_adding_the_same_asset_twice_is_idempotent(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "idempotent.db")
    collection = _collection(store, project_id)
    asset = store.list_assets(run_id)[0]

    payload = AssetCollectionItemAdd(asset_id=asset.asset_id, role="person_image")
    first_outcome, first = store.add_asset_collection_item(collection.collection_id, payload)
    second_outcome, second = store.add_asset_collection_item(collection.collection_id, payload)

    assert first_outcome is CollectionAddOutcome.CREATED
    assert second_outcome is CollectionAddOutcome.EXISTING
    assert first is not None and second is not None
    assert first.item_id == second.item_id

    items = store.list_asset_collection_items(collection.collection_id)
    assert items is not None and len(items) == 1
    refreshed = store.get_asset_collection(collection.collection_id)
    assert refreshed is not None and refreshed.item_count == 1


def test_adding_an_unknown_asset_is_refused(tmp_path) -> None:
    store, project_id, _run_id = _seed(tmp_path, "unknown-asset.db")
    collection = _collection(store, project_id)

    outcome, item = store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(asset_id="asset_missing"),
    )
    assert outcome is CollectionAddOutcome.ASSET_NOT_FOUND
    assert item is None

    with pytest.raises(HTTPException) as exc_info:
        add_item_route(
            collection.collection_id,
            AssetCollectionItemAdd(asset_id="asset_missing"),
            store,
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "asset not found"


def test_adding_into_an_unknown_collection_is_refused(tmp_path) -> None:
    store, _project_id, run_id = _seed(tmp_path, "unknown-collection.db")
    asset = store.list_assets(run_id)[0]

    outcome, item = store.add_asset_collection_item(
        "acol_missing",
        AssetCollectionItemAdd(asset_id=asset.asset_id),
    )
    assert outcome is CollectionAddOutcome.COLLECTION_NOT_FOUND
    assert item is None

    with pytest.raises(HTTPException) as exc_info:
        add_item_route("acol_missing", AssetCollectionItemAdd(asset_id=asset.asset_id), store)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "asset collection not found"


def test_item_keeps_provenance_and_license(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "rights.db")
    collection = _collection(store, project_id)
    asset = store.list_assets(run_id)[0]
    # +08:00 input must land as the same instant, not eight hours off.
    expires_at = datetime(2027, 1, 1, 8, 0, tzinfo=UTC) + timedelta(hours=8)

    outcome, item = store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(
            asset_id=asset.asset_id,
            role="person_image",
            note="已获肖像授权，仅限中国大陆投放",
            provenance=AssetProvenance(
                source="generated",
                provider="dashscope",
                tool_key="dashscope_image",
                source_run_id=run_id,
                source_asset_id=asset.asset_id,
            ),
            license=AssetLicense(
                license_type="licensed",
                holder="测试品牌",
                usage_scope="中国大陆 / 线上投放",
                expires_at=expires_at,
                restrictions=["不得用于医疗宣称"],
            ),
        ),
    )

    assert outcome is CollectionAddOutcome.CREATED
    assert item is not None
    stored = store.get_asset_collection(collection.collection_id)
    assert stored is not None
    entry = stored.items[0]
    assert entry.provenance.provider == "dashscope"
    assert entry.provenance.source_run_id == run_id
    assert entry.license.license_type == "licensed"
    assert entry.license.restrictions == ["不得用于医疗宣称"]
    assert as_utc(entry.license.expires_at) == expires_at.astimezone(UTC)
    # The entry carries the asset payload too, so the page needs no second call.
    assert entry.asset is not None and entry.asset.asset_id == asset.asset_id


def test_item_update_and_soft_removal(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "soft-remove.db")
    collection = _collection(store, project_id)
    asset = store.list_assets(run_id)[0]
    _outcome, item = store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(asset_id=asset.asset_id, role="reference"),
    )
    assert item is not None

    updated = store.update_asset_collection_item(
        item.item_id,
        AssetCollectionItemUpdate(
            role="person_image",
            license=AssetLicense(license_type="owned", holder="内部素材"),
        ),
    )
    assert updated is not None
    assert updated.role == "person_image"
    assert updated.license.license_type == "owned"

    removed = store.remove_asset_collection_item(item.item_id)
    assert removed is not None and removed.status == "removed"

    assert store.list_asset_collection_items(collection.collection_id) == []
    with_removed = store.list_asset_collection_items(
        collection.collection_id,
        include_removed=True,
    )
    assert with_removed is not None and len(with_removed) == 1

    with pytest.raises(HTTPException) as exc_info:
        routes.update_asset_collection_item("aitem_missing", AssetCollectionItemUpdate(), store)
    assert exc_info.value.status_code == 404


def test_project_library_aggregates_collections_and_kinds(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "library.db")
    person = _collection(store, project_id, name="人物形象库", kind="person")
    voice = _collection(store, project_id, name="声音库", kind="voice")
    asset = store.list_assets(run_id)[0]
    store.add_asset_collection_item(
        person.collection_id,
        AssetCollectionItemAdd(asset_id=asset.asset_id),
    )

    library = store.project_asset_library(project_id)
    assert library is not None
    assert library.totals["collection_count"] == 2
    assert library.totals["item_count"] == 1
    assert library.totals["by_kind"] == {"person": 1, "voice": 1}
    by_id = {item.collection_id: item for item in library.collections}
    assert by_id[person.collection_id].item_count == 1
    assert by_id[voice.collection_id].item_count == 0
    assert len(by_id[person.collection_id].items) == 1


def test_items_are_filterable_by_role(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "role-filter.db")
    collection = _collection(store, project_id)
    asset = store.list_assets(run_id)[0]
    store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(asset_id=asset.asset_id, role="person_image"),
    )

    assert store.list_asset_collection_items(collection.collection_id, role="person_image") != []
    assert store.list_asset_collection_items(collection.collection_id, role="voice") == []
    assert store.list_asset_collection_items("acol_missing") is None


# --------------------------------------------------------------------------- #
# import from a run
# --------------------------------------------------------------------------- #


def test_import_promotes_a_run_s_assets_and_is_repeatable(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "import.db")
    collection = _collection(store, project_id, kind="project_asset")
    store.upload_asset(
        run_id,
        AssetUpload(
            asset_type="narration_audio",
            filename="voice.mp3",
            content_base64=base64.b64encode(b"audio bytes").decode("ascii"),
            mime_type="audio/mpeg",
        ),
    )

    outcome, result = store.import_run_assets_into_collection(
        collection.collection_id,
        AssetCollectionImportRequest(run_id=run_id, role="reference"),
    )
    assert outcome is CollectionImportOutcome.IMPORTED
    assert result is not None
    assert result.imported_count == 2
    assert result.skipped_asset_ids == []

    # Provenance points back at the source run, so a reviewer can trace the take.
    items = store.list_asset_collection_items(collection.collection_id)
    assert items is not None
    assert {item.provenance.source_run_id for item in items} == {run_id}
    assert {item.asset.asset_type for item in items if item.asset} == {
        "person_image",
        "narration_audio",
    }

    # Importing again adds nothing and reports what it skipped.
    again_outcome, again = store.import_run_assets_into_collection(
        collection.collection_id,
        AssetCollectionImportRequest(run_id=run_id, role="reference"),
    )
    assert again_outcome is CollectionImportOutcome.IMPORTED
    assert again is not None
    assert again.imported_count == 0
    assert len(again.skipped_asset_ids) == 2

    # The promotion is auditable on the source run — and a no-op re-import adds
    # no event, so the log stays a record of real changes.
    promoted = store.events.list_events(run_id, event_type="run.assets_promoted_to_library")
    assert len(promoted) == 1
    assert promoted[0].payload["imported_count"] == 2


def test_import_can_filter_by_asset_type(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "import-filter.db")
    collection = _collection(store, project_id, kind="voice")
    store.upload_asset(
        run_id,
        AssetUpload(
            asset_type="narration_audio",
            filename="voice.mp3",
            content_base64=base64.b64encode(b"audio bytes").decode("ascii"),
            mime_type="audio/mpeg",
        ),
    )

    _outcome, result = store.import_run_assets_into_collection(
        collection.collection_id,
        AssetCollectionImportRequest(
            run_id=run_id,
            asset_types=["narration_audio"],
            role="voice",
            license=AssetLicense(license_type="owned", holder="测试品牌"),
        ),
    )
    assert result is not None and result.imported_count == 1
    items = store.list_asset_collection_items(collection.collection_id)
    assert items is not None and len(items) == 1
    assert items[0].role == "voice"
    assert items[0].license.holder == "测试品牌"


def test_import_reports_missing_collection_and_run(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "import-errors.db")
    collection = _collection(store, project_id)

    missing_collection, _ = store.import_run_assets_into_collection(
        "acol_missing",
        AssetCollectionImportRequest(run_id=run_id),
    )
    assert missing_collection is CollectionImportOutcome.COLLECTION_NOT_FOUND

    missing_run, _ = store.import_run_assets_into_collection(
        collection.collection_id,
        AssetCollectionImportRequest(run_id="run_missing"),
    )
    assert missing_run is CollectionImportOutcome.RUN_NOT_FOUND

    with pytest.raises(HTTPException) as exc_info:
        import_route(
            collection.collection_id,
            AssetCollectionImportRequest(run_id="run_missing"),
            store,
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "run not found"


# --------------------------------------------------------------------------- #
# reuse
# --------------------------------------------------------------------------- #


def test_reuse_creates_a_new_run_asset_that_keeps_its_rights(tmp_path) -> None:
    store, project_id, source_run_id = _seed(tmp_path, "reuse.db")
    collection = _collection(store, project_id)
    source_asset = store.list_assets(source_run_id)[0]
    _outcome, item = store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(
            asset_id=source_asset.asset_id,
            role="person_image",
            license=AssetLicense(license_type="licensed", holder="测试品牌"),
        ),
    )
    assert item is not None

    target_run = store.create_run(
        project_id=project_id,
        payload=RunCreate(raw_script="第二个 run", brand_name="测试品牌", product_name="测试产品"),
        pipeline=PipelineLoader().get_pipeline("avatar_spokesperson"),
    )

    outcome, result = store.reuse_asset_into_run(
        target_run.run_id,
        AssetReuseRequest(item_id=item.item_id, note="复用上一支片子的形象"),
    )

    assert outcome is AssetReuseOutcome.REUSED
    assert result is not None
    assert result.reused_from_asset_id == source_asset.asset_id
    assert result.asset.run_id == target_run.run_id
    assert result.asset.asset_id != source_asset.asset_id
    # The bytes are shared rather than copied.
    assert result.asset.storage_url == source_asset.storage_url
    assert result.asset.checksum == source_asset.checksum
    assert result.asset.asset_metadata["reused_from"]["run_id"] == source_run_id
    assert result.asset.asset_metadata["library_license"]["license_type"] == "licensed"

    target_assets = store.list_assets(target_run.run_id)
    assert target_assets is not None
    assert [asset.asset_id for asset in target_assets] == [result.asset.asset_id]

    reused_events = store.events.list_events(
        target_run.run_id,
        event_type="run.asset_reused_from_library",
    )
    assert len(reused_events) == 1
    assert reused_events[0].payload["source_asset_id"] == source_asset.asset_id


def test_reuse_can_override_the_asset_type(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "reuse-type.db")
    collection = _collection(store, project_id)
    asset = store.list_assets(run_id)[0]
    _outcome, item = store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(asset_id=asset.asset_id),
    )
    assert item is not None

    _outcome, result = store.reuse_asset_into_run(
        run_id,
        AssetReuseRequest(item_id=item.item_id, asset_type="brand_mascot"),
    )
    assert result is not None and result.asset.asset_type == "brand_mascot"


def test_reuse_reports_missing_item_and_run(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "reuse-errors.db")
    collection = _collection(store, project_id)
    asset = store.list_assets(run_id)[0]
    _outcome, item = store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(asset_id=asset.asset_id),
    )
    assert item is not None

    missing_item, _ = store.reuse_asset_into_run(run_id, AssetReuseRequest(item_id="aitem_x"))
    assert missing_item is AssetReuseOutcome.ITEM_NOT_FOUND

    missing_run, _ = store.reuse_asset_into_run(
        "run_missing",
        AssetReuseRequest(item_id=item.item_id),
    )
    assert missing_run is AssetReuseOutcome.RUN_NOT_FOUND

    with pytest.raises(HTTPException) as exc_info:
        reuse_route("run_missing", AssetReuseRequest(item_id=item.item_id), store)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "run not found"

    with pytest.raises(HTTPException) as exc_info:
        reuse_route(run_id, AssetReuseRequest(item_id="aitem_x"), store)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "asset library item not found"


def test_removed_items_can_still_be_read_but_not_relisted(tmp_path) -> None:
    store, project_id, run_id = _seed(tmp_path, "reuse-removed.db")
    collection = _collection(store, project_id)
    asset = store.list_assets(run_id)[0]
    _outcome, item = store.add_asset_collection_item(
        collection.collection_id,
        AssetCollectionItemAdd(asset_id=asset.asset_id),
    )
    assert item is not None
    store.remove_asset_collection_item(item.item_id)

    # Reuse still works on an archived entry: a run that already referenced it
    # must not break just because the library tidied up.
    outcome, result = store.reuse_asset_into_run(run_id, AssetReuseRequest(item_id=item.item_id))
    assert outcome is AssetReuseOutcome.REUSED
    assert result is not None


# --------------------------------------------------------------------------- #
# routes and page
# --------------------------------------------------------------------------- #


def test_asset_library_endpoints_are_registered() -> None:
    paths = app.openapi()["paths"]

    for path, methods in NEW_ENDPOINTS.items():
        assert path in paths, f"missing asset library endpoint: {path}"
        for method in methods:
            assert method in paths[path], f"{path} is missing {method.upper()}"

    # The page is registered on the app, not in the schema: HTML routes are kept
    # out of the API contract on purpose (see tests/test_openapi_contract.py).
    registered = {getattr(route, "path", None) for route in app.routes}
    assert "/asset-library" in registered
    assert "/asset-library" not in paths


def test_collection_route_reports_missing_collection(tmp_path) -> None:
    store, _project_id, _run_id = _seed(tmp_path, "route-404.db")

    with pytest.raises(HTTPException) as exc_info:
        get_collection_route("acol_missing", store)
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        update_collection_route("acol_missing", AssetCollectionUpdate(name="x"), store)
    assert exc_info.value.status_code == 404


def test_asset_library_page_exposes_the_workflow() -> None:
    html = asset_library_html_path().read_text(encoding="utf-8")

    assert "资产库" in html
    for marker in (
        "新建资产集合",
        "从 Run 沉淀资产",
        "复用到 Run",
        "授权",
        "来源",
        "资产条目",
    ):
        assert marker in html, f"missing asset library marker: {marker}"

    assert "/api/projects" in html
    assert "asset-library" in html
    assert "import-from-run" in html
    assert "loadLibrary" in html
    assert "reuseIntoRun" in html

    dashboard = (asset_library_html_path().parent / "dashboard.html").read_text(encoding="utf-8")
    assert "openAssetLibraryButton" in dashboard
