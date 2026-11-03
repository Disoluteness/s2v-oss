#!/usr/bin/env python3
"""Script in, final video out.

    SCRIPT2VIDEO_BASE_URL=http://127.0.0.1:8080 \
    SCRIPT2VIDEO_API_KEY=s2v_... \
    python examples/script_to_video.py

Without an API key the client falls back to ``X-Actor-Id`` (set
``SCRIPT2VIDEO_ACTOR_ID``) or to the deployment's default actor, which is how the
internal gateway path is expected to work.
"""

from __future__ import annotations

import sys

from s2v import ApiError, FinalVideoNotFound, Script2VideoClient

SCRIPT = (
    "开场三秒用一个反常识的问题抓住注意力。"
    "然后介绍产品解决了什么具体问题，给出一个可验证的数据。"
    "最后用一句有记忆点的行动号召收尾。"
)


def main() -> int:
    client = Script2VideoClient()

    identity = client.me()
    print(f"acting as {identity.actor_id} (source={identity.source})")
    print(f"permissions: {len(identity.permissions)}")

    try:
        video = client.script_to_video(
            SCRIPT,
            project_title="示例：新品预热",
            timeout=1800,
            accept_paid_providers=True,
            on_progress=lambda run: print(f"  ... {run.status} @ {run.current_stage_key}"),
        )
    except FinalVideoNotFound as exc:
        print(f"no video was produced: {exc}")
        return 1
    except ApiError as exc:
        print(f"API refused the request: HTTP {exc.status} {exc.code} — {exc.message}")
        print(f"  retryable: {exc.retryable}")
        return 1

    print(f"\nfinal video: {video.url}")
    print(f"  asset: {video.asset_id} ({video.asset_type}, {video.mime_type})")
    print(f"  duration: {video.duration_ms} ms  size: {video.file_size} bytes")

    payload = client.download_asset(video.asset_id)
    out = "out.mp4"
    with open(out, "wb") as handle:
        handle.write(payload)
    print(f"  saved {len(payload)} bytes to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
