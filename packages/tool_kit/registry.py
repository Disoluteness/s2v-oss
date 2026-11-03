from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from dotenv import dotenv_values

from packages.tool_kit.contracts import (
    RuntimeType,
    ToolCapabilitySummary,
    ToolDefinition,
    ToolDependency,
    ToolProviderSummary,
    ToolStatus,
)

BinaryChecker = Callable[[str], str | None]


class ToolRegistry:
    """Runtime registry that resolves catalog definitions into availability status."""

    def __init__(
        self,
        *,
        catalog_root: Path | None = None,
        catalog_paths: Iterable[Path] | None = None,
        env_file: Path | None = None,
        env: Mapping[str, str | None] | None = None,
        env_overrides: Mapping[str, str | None] | None = None,
        disabled_providers: Iterable[str] | None = None,
        binary_checker: BinaryChecker | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.catalog_root = catalog_root or project_root / "tool_catalog"
        self.catalog_paths = list(catalog_paths) if catalog_paths is not None else None
        self.env_file = env_file or project_root / ".env"
        self.env = dict(env) if env is not None else self._load_environment()
        if env_overrides:
            self.env.update(dict(env_overrides))
        self.disabled_providers = set(disabled_providers or [])
        self.binary_checker = binary_checker or shutil.which

    def discover(self) -> list[ToolDefinition]:
        return [self._resolve_status(definition) for definition in self._load_definitions()]

    def list_tools(
        self,
        *,
        capability: str | None = None,
        provider: str | None = None,
        status: ToolStatus | None = None,
    ) -> list[ToolDefinition]:
        tools = self.discover()
        if capability is not None:
            tools = [tool for tool in tools if tool.capability == capability]
        if provider is not None:
            tools = [tool for tool in tools if tool.provider == provider]
        if status is not None:
            tools = [tool for tool in tools if tool.status == status]
        return sorted(tools, key=lambda tool: (tool.capability, tool.provider, tool.tool_key))

    def get_tool(self, tool_key: str) -> ToolDefinition | None:
        return next((tool for tool in self.discover() if tool.tool_key == tool_key), None)

    def list_capabilities(self) -> list[ToolCapabilitySummary]:
        summaries = []
        by_capability: dict[str, list[ToolDefinition]] = {}
        for tool in self.discover():
            by_capability.setdefault(tool.capability, []).append(tool)

        for capability, tools in sorted(by_capability.items()):
            summaries.append(
                ToolCapabilitySummary(
                    capability=capability,
                    total=len(tools),
                    available=self._count_status(tools, ToolStatus.AVAILABLE),
                    degraded=self._count_status(tools, ToolStatus.DEGRADED),
                    unavailable=self._count_status(tools, ToolStatus.UNAVAILABLE),
                    providers=sorted({tool.provider for tool in tools}),
                    available_providers=sorted(
                        {tool.provider for tool in tools if tool.status == ToolStatus.AVAILABLE}
                    ),
                    tool_keys=sorted(tool.tool_key for tool in tools),
                    fallback_tool_keys=sorted(
                        {
                            fallback
                            for tool in tools
                            for fallback in tool.fallback_tool_keys
                        }
                    ),
                )
            )
        return summaries

    def list_providers(self) -> list[ToolProviderSummary]:
        summaries = []
        by_provider: dict[str, list[ToolDefinition]] = {}
        for tool in self.discover():
            by_provider.setdefault(tool.provider, []).append(tool)

        for provider, tools in sorted(by_provider.items()):
            summaries.append(
                ToolProviderSummary(
                    provider=provider,
                    total=len(tools),
                    available=self._count_status(tools, ToolStatus.AVAILABLE),
                    degraded=self._count_status(tools, ToolStatus.DEGRADED),
                    unavailable=self._count_status(tools, ToolStatus.UNAVAILABLE),
                    capabilities=sorted({tool.capability for tool in tools}),
                    tool_keys=sorted(tool.tool_key for tool in tools),
                )
            )
        return summaries

    def _load_definitions(self) -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for path in self._catalog_paths():
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            entries = data["tools"] if isinstance(data, dict) else data
            definitions.extend(ToolDefinition.model_validate(entry) for entry in entries)
        return definitions

    def _catalog_paths(self) -> list[Path]:
        if self.catalog_paths is not None:
            return self.catalog_paths
        if not self.catalog_root.exists():
            return []
        return sorted(self.catalog_root.glob("*.json"))

    def _resolve_status(self, definition: ToolDefinition) -> ToolDefinition:
        if definition.provider in self.disabled_providers:
            return definition.model_copy(
                update={
                    "status": ToolStatus.UNAVAILABLE,
                    "missing_dependencies": [f"provider_disabled:{definition.provider}"],
                    "status_reason": (
                        f"Provider is disabled by credential manager: {definition.provider}"
                    ),
                }
            )
        missing = [
            self._missing_dependency_key(dependency)
            for dependency in definition.dependencies
            if dependency.required and self._dependency_missing(dependency)
        ]
        if missing:
            reason = "Missing dependencies: " + ", ".join(missing)
            status = ToolStatus.UNAVAILABLE
        elif definition.status == ToolStatus.DEGRADED:
            reason = definition.status_reason or "Tool is available with degraded capability."
            status = ToolStatus.DEGRADED
        else:
            reason = definition.status_reason or self._available_reason(definition.runtime_type)
            status = ToolStatus.AVAILABLE
        return definition.model_copy(
            update={
                "status": status,
                "missing_dependencies": missing,
                "status_reason": reason,
            }
        )

    def _dependency_missing(self, dependency: ToolDependency) -> bool:
        if dependency.kind == "env":
            return not self.env.get(dependency.name)
        if dependency.kind == "binary":
            return self.binary_checker(dependency.name) is None
        return False

    def _missing_dependency_key(self, dependency: ToolDependency) -> str:
        return f"{dependency.kind}:{dependency.name}"

    def _load_environment(self) -> dict[str, str | None]:
        file_values = dotenv_values(self.env_file) if self.env_file.is_file() else {}
        return {**file_values, **os.environ}

    def _available_reason(self, runtime_type: RuntimeType) -> str:
        if runtime_type == RuntimeType.API:
            return "Required API configuration is present."
        if runtime_type == RuntimeType.LOCAL:
            return "Required local runtime dependencies are present."
        return "Required hybrid runtime dependencies are present."

    def _count_status(self, tools: list[ToolDefinition], status: ToolStatus) -> int:
        return sum(1 for tool in tools if tool.status == status)
