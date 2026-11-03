"""Read-only views over the API's JSON objects.

Every value the client returns is a :class:`Model` subclass: the fields the API
documents are exposed as real attributes with the right names, and anything the
server adds later is still reachable through ``model["new_field"]`` or
``model.get("new_field")``. That combination is deliberate — a client that
predates a server-side addition keeps working, and one that wants the new field
does not need a client release to see it.
"""

from __future__ import annotations

from typing import Any

Json = dict[str, Any]

#: Statuses after which a run will not change again on its own.
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
#: A run parked on a human: not finished, but nothing will happen without one.
BLOCKING_RUN_STATUSES: frozenset[str] = frozenset({"waiting_review", "paused"})
#: Asset types that count as a deliverable, most preferred first.
FINAL_VIDEO_ASSET_TYPES: tuple[str, ...] = ("rendered_video", "edited_video")


class Model:
    """A thin view over a JSON object, with attribute access for known fields."""

    def __init__(self, data: Json | None = None) -> None:
        self.raw: Json = dict(data or {})

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def __contains__(self, key: object) -> bool:
        return key in self.raw

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def to_dict(self) -> Json:
        return dict(self.raw)

    def __repr__(self) -> str:
        summary = ", ".join(f"{key}={value!r}" for key, value in list(self.raw.items())[:4])
        return f"{type(self).__name__}({summary})"


class Project(Model):
    @property
    def project_id(self) -> str | None:
        return self.raw.get("project_id")

    @property
    def title(self) -> str | None:
        return self.raw.get("title")


class Stage(Model):
    @property
    def stage_key(self) -> str | None:
        return self.raw.get("stage_key")

    @property
    def status(self) -> str | None:
        return self.raw.get("status")

    @property
    def requires_human_review(self) -> bool:
        return bool(self.raw.get("requires_human_review"))


class Run(Model):
    @property
    def run_id(self) -> str | None:
        return self.raw.get("run_id")

    @property
    def project_id(self) -> str | None:
        return self.raw.get("project_id")

    @property
    def status(self) -> str | None:
        return self.raw.get("status")

    @property
    def current_stage_key(self) -> str | None:
        return self.raw.get("current_stage_key")

    @property
    def pipeline_key(self) -> str | None:
        return self.raw.get("pipeline_key")

    @property
    def stages(self) -> list[Stage]:
        return [Stage(item) for item in self.raw.get("stages") or []]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def is_blocked_on_human(self) -> bool:
        """True when the run will not progress without review or a resume."""
        return self.status in BLOCKING_RUN_STATUSES


class Asset(Model):
    @property
    def asset_id(self) -> str | None:
        return self.raw.get("asset_id")

    @property
    def run_id(self) -> str | None:
        return self.raw.get("run_id")

    @property
    def asset_type(self) -> str | None:
        return self.raw.get("asset_type")

    @property
    def mime_type(self) -> str | None:
        return self.raw.get("mime_type")

    @property
    def storage_url(self) -> str | None:
        return self.raw.get("storage_url")


class Artifact(Model):
    @property
    def artifact_id(self) -> str | None:
        return self.raw.get("artifact_id")

    @property
    def artifact_type(self) -> str | None:
        return self.raw.get("artifact_type")

    @property
    def run_id(self) -> str | None:
        return self.raw.get("run_id")

    @property
    def payload(self) -> Json:
        return dict(self.raw.get("payload") or {})


class Job(Model):
    @property
    def job_id(self) -> str | None:
        return self.raw.get("job_id")

    @property
    def run_id(self) -> str | None:
        return self.raw.get("run_id")

    @property
    def status(self) -> str | None:
        return self.raw.get("status")

    @property
    def job_type(self) -> str | None:
        return self.raw.get("job_type")


class FinalVideo(Model):
    """The render output of a run, as reported by the run board.

    ``download_url`` is a signed, time-limited link, so it is worth re-reading
    the board rather than caching this object for long.
    """

    @property
    def asset_id(self) -> str | None:
        return self.raw.get("asset_id")

    @property
    def asset_type(self) -> str | None:
        return self.raw.get("asset_type")

    @property
    def mime_type(self) -> str | None:
        return self.raw.get("mime_type")

    @property
    def download_url(self) -> str | None:
        return self.raw.get("download_url")

    @property
    def url(self) -> str | None:
        """Alias for ``download_url``; the link is the thing callers want."""
        return self.raw.get("download_url")

    @property
    def duration_ms(self) -> int | None:
        return self.raw.get("duration_ms")

    @property
    def width(self) -> int | None:
        return self.raw.get("width")

    @property
    def height(self) -> int | None:
        return self.raw.get("height")

    @property
    def file_size(self) -> int | None:
        return self.raw.get("file_size")


class Actor(Model):
    @property
    def actor_id(self) -> str | None:
        return self.raw.get("actor_id")

    @property
    def source(self) -> str | None:
        return self.raw.get("source")

    @property
    def permissions(self) -> list[str]:
        return list(self.raw.get("permissions") or [])

    def can(self, permission: str) -> bool:
        return permission in self.permissions


class ApiKey(Model):
    @property
    def api_key_id(self) -> str | None:
        return self.raw.get("api_key_id")

    @property
    def name(self) -> str | None:
        return self.raw.get("name")

    @property
    def key_prefix(self) -> str | None:
        return self.raw.get("key_prefix")

    @property
    def status(self) -> str | None:
        return self.raw.get("status")

    @property
    def scopes(self) -> list[str] | None:
        scopes = self.raw.get("scopes")
        return list(scopes) if scopes is not None else None

    @property
    def plaintext(self) -> str | None:
        """The credential itself — present only on the creation response."""
        return self.raw.get("api_key")


class WebhookSubscription(Model):
    """A registered endpoint. ``secret`` is shown on every read on purpose: it
    authenticates the platform *to* the receiver, so there is nothing to hide
    from the person who just read it back."""

    @property
    def subscription_id(self) -> str | None:
        return self.raw.get("subscription_id")

    @property
    def name(self) -> str | None:
        return self.raw.get("name")

    @property
    def url(self) -> str | None:
        return self.raw.get("url")

    @property
    def secret(self) -> str | None:
        """Key for :func:`s2v.verify_webhook_signature` on received deliveries."""
        return self.raw.get("secret")

    @property
    def event_types(self) -> list[str]:
        return list(self.raw.get("event_types") or [])

    @property
    def status(self) -> str | None:
        return self.raw.get("status")

    @property
    def consecutive_failures(self) -> int:
        return int(self.raw.get("consecutive_failures") or 0)


class WebhookDelivery(Model):
    """One queued or attempted delivery, for debugging and manual replay."""

    @property
    def delivery_id(self) -> str | None:
        return self.raw.get("delivery_id")

    @property
    def subscription_id(self) -> str | None:
        return self.raw.get("subscription_id")

    @property
    def event_id(self) -> str | None:
        return self.raw.get("event_id")

    @property
    def event_type(self) -> str | None:
        return self.raw.get("event_type")

    @property
    def run_id(self) -> str | None:
        return self.raw.get("run_id")

    @property
    def status(self) -> str | None:
        return self.raw.get("status")

    @property
    def attempt(self) -> int:
        return int(self.raw.get("attempt") or 0)

    @property
    def max_attempts(self) -> int:
        return int(self.raw.get("max_attempts") or 0)

    @property
    def error_message(self) -> str | None:
        return self.raw.get("error_message")

    @property
    def response_status(self) -> int | None:
        return self.raw.get("response_status")


class Page(Model):
    """A page of results, with items optionally wrapped in a model.

    ``model`` converts each entry on the way in, so ``page.items`` holds real
    objects (``ApiKey``, ...) rather than raw dicts while ``page.raw`` still has
    the untouched envelope.
    """

    def __init__(
        self,
        data: Json | None = None,
        *,
        model: type[Model] | None = None,
    ) -> None:
        super().__init__(data)
        if model is not None:
            self.raw["items"] = [model(item) for item in self.raw.get("items") or []]

    @property
    def items(self) -> list[Any]:
        return list(self.raw.get("items") or [])

    @property
    def total(self) -> int:
        return int(self.raw.get("total") or 0)

    @property
    def limit(self) -> int:
        return int(self.raw.get("limit") or 0)

    @property
    def offset(self) -> int:
        return int(self.raw.get("offset") or 0)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)
