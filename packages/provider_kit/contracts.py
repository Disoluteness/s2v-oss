from typing import Any, Protocol

from packages.tool_kit.contracts import ToolExecutionResult


class ProviderAdapter(Protocol):
    provider_key: str

    def get_capabilities(self) -> list[str]:
        ...

    def check_availability(self) -> bool:
        ...

    def estimate_cost(self, capability: str, payload: dict[str, Any]) -> float:
        ...

    def validate_input(self, capability: str, payload: dict[str, Any]) -> None:
        ...

    def execute(self, capability: str, payload: dict[str, Any]) -> ToolExecutionResult:
        ...

    def normalize_output(self, capability: str, raw_response: dict[str, Any]) -> dict[str, Any]:
        ...

    def cancel(self, external_task_id: str) -> None:
        ...

    def get_status(self, external_task_id: str) -> dict[str, Any]:
        ...

