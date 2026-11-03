from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from packages.pipeline_kit.models import PipelineDefinition


class PipelineNotFoundError(ValueError):
    pass


class PipelineLoader:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[2] / "pipelines"

    def list_pipelines(self) -> list[PipelineDefinition]:
        return [self.get_pipeline(path.stem) for path in sorted(self.root.glob("*.json"))]

    def get_pipeline(self, pipeline_key: str) -> PipelineDefinition:
        return _load_pipeline(self.root, pipeline_key)


@lru_cache(maxsize=64)
def _load_pipeline(root: Path, pipeline_key: str) -> PipelineDefinition:
    path = root / f"{pipeline_key}.json"
    if not path.is_file():
        raise PipelineNotFoundError(f"Pipeline not found: {pipeline_key}")
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if "extends" in data:
        base_key = data["extends"]
        if not isinstance(base_key, str) or not base_key:
            raise ValueError(f"Invalid pipeline extends value in {path.name}")
        base_snapshot = deepcopy(_load_pipeline(root, base_key).snapshot())
        base_snapshot.pop("content_hash", None)
        overrides = {key: value for key, value in data.items() if key != "extends"}
        data = _deep_merge(base_snapshot, overrides)
    pipeline = PipelineDefinition.model_validate(data)
    pipeline.content_hash = pipeline.stable_content_hash()
    return pipeline


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
