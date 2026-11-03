"""The version the API publishes, written down once.

It lives in its own module because two very different layers need it: the app
factory puts it in the OpenAPI document (``info.version``), and the deployment
posture report hands it back to an operator asking "what is running here". The
first is a composition-root concern and the second is a storage concern, so
neither can own it without the other importing across a boundary it should not
cross.

Deliberately a constant rather than a setting: it describes the build, and a
value an operator can override from the environment is a value an operator can
get wrong. ``APP_VERSION`` is the API contract's version.

``1.0.0`` is a **promise, not a progress report**. Until V8.5 the number tracked
the roadmap (the V8 line closed inside the ``0.9`` series), which was the honest
thing to publish while the HTTP contract was still being written. V1.0 is where
those two ideas come apart: the roadmap is done, and ``docs/API_STABILITY.md``
now says what may change without notice and what may not. Bumping the major
version is how a client learns that the stability policy is in force — leaving it
in the ``0.9`` series would tell integrators, correctly, to expect the shape to
move.

``1.1.0`` is the first minor bump under that policy, and it is the policy's
first proof: every V1.1 change (auth sessions, tenant scoping, list sorting,
new pipelines, richer media analysis) is additive — new endpoints, new optional
inputs, new error codes, new catalogue entries. No stable-surface shape moved,
so a client written against ``1.0.0`` works against ``1.1.0`` unchanged.

The SDKs carry their own independent versions; a client version is not a
statement about this server.
"""

from __future__ import annotations

APP_VERSION = "1.1.0"
