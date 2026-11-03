"""Script2Video Studio Python SDK (V7.2).

A dependency-free client for the platform's HTTP API. It covers the whole
script-to-video path —::

    from s2v import Script2VideoClient

    client = Script2VideoClient("https://studio.internal", api_key="s2v_...")

    video = client.script_to_video(
        "开场用一个反常识的问题，然后给出一个可验证的数据，最后收尾。",
        project_title="新品预热",
    )
    print(video.url)

— and the individual steps underneath it, so an internal platform can plug in at
whatever level it needs: create a run, upload the persona image, start it, watch
it, fetch the render, or retry a failed stage.

The name ``s2v`` is the import name; the distribution is published as
``s2v-client``.
"""

from s2v.client import Script2VideoClient
from s2v.errors import (
    RETRYABLE_CODES,
    ApiError,
    FinalVideoNotFound,
    RunWaitTimeout,
    Script2VideoError,
    TransportError,
)
from s2v.models import (
    BLOCKING_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    Actor,
    ApiKey,
    Artifact,
    Asset,
    FinalVideo,
    Job,
    Model,
    Page,
    Project,
    Run,
    Stage,
    WebhookDelivery,
    WebhookSubscription,
)
from s2v.transport import Response, Transport, UrllibTransport
from s2v.webhooks import (
    ATTEMPT_HEADER,
    DELIVERY_ID_HEADER,
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    SIGNATURE_HEADER,
    SIGNATURE_TOLERANCE_SECONDS,
    compute_signature,
    parse_signature_header,
    verify_webhook_signature,
)

__version__ = "0.8.0"

__all__ = [
    "ATTEMPT_HEADER",
    "BLOCKING_RUN_STATUSES",
    "DELIVERY_ID_HEADER",
    "EVENT_ID_HEADER",
    "EVENT_TYPE_HEADER",
    "RETRYABLE_CODES",
    "SIGNATURE_HEADER",
    "SIGNATURE_TOLERANCE_SECONDS",
    "TERMINAL_RUN_STATUSES",
    "Actor",
    "ApiError",
    "ApiKey",
    "Artifact",
    "Asset",
    "FinalVideo",
    "FinalVideoNotFound",
    "Job",
    "Model",
    "Page",
    "Project",
    "Response",
    "Run",
    "RunWaitTimeout",
    "Script2VideoClient",
    "Script2VideoError",
    "Stage",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "WebhookDelivery",
    "WebhookSubscription",
    "__version__",
    "compute_signature",
    "parse_signature_header",
    "verify_webhook_signature",
]
