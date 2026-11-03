from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
from collections.abc import Callable
from threading import Event

from sqlalchemy.orm import Session

from apps.api.app.config import settings
from apps.api.app.db import SessionLocal
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import BackgroundJob, BackgroundJobStatus
from apps.api.app.observability import (
    REGISTRY,
    WORKER_JOB_DURATION,
    WORKER_JOBS_TOTAL,
    configure_logging,
)
from apps.api.app.webhook_dispatch import WebhookDispatcher

logger = logging.getLogger("script2video.worker.background_jobs")

SessionFactory = Callable[[], Session]
StoreFactory = Callable[[Session], DatabaseStore]

#: How a finished job's status maps onto the ``s2v_worker_jobs_total`` counter
#: (V8.2): one outcome label per V8.1 disposition plus the provider wait.
OUTCOME_FOR_STATUS: dict[str, str] = {
    BackgroundJobStatus.COMPLETED.value: "processed",
    BackgroundJobStatus.WAITING_PROVIDER.value: "waiting_provider",
    BackgroundJobStatus.FAILED.value: "failed",
    BackgroundJobStatus.QUEUED.value: "retried",
    BackgroundJobStatus.DEAD.value: "dead_lettered",
    BackgroundJobStatus.CANCELLED.value: "cancelled",
}

# V8.1 worker roles: which queues a worker claims. ``all`` (the default) keeps
# a single-instance deployment behaving exactly as before; a role is the
# deployment lever for splitting heavy ffmpeg work away from provider polling.
# There is deliberately no ``planner`` role yet — planning is synchronous and
# no job type maps to it.
ROLE_QUEUES: dict[str, list[str] | None] = {
    "all": None,
    "provider": ["provider"],
    "analysis": ["analysis"],
    "media": ["media"],
    "render": ["render"],
}
DEFAULT_ROLE = "all"


def queues_for_role(role: str) -> list[str] | None:
    queues = ROLE_QUEUES.get(role)
    if queues is None and role != DEFAULT_ROLE:
        raise ValueError(f"unknown worker role: {role}")
    return queues


class BackgroundJobWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        store_factory: StoreFactory = DatabaseStore,
        batch_size: int = 5,
        poll_interval_seconds: float = 5.0,
        lease_seconds: int = 300,
        worker_id: str | None = None,
        purge_interval_seconds: float = 300.0,
        webhook_dispatcher: WebhookDispatcher | None = None,
        role: str = DEFAULT_ROLE,
        job_timeout_seconds: int = 1800,
        reap_interval_seconds: float = 60.0,
    ) -> None:
        self.session_factory = session_factory
        self.store_factory = store_factory
        self.batch_size = max(1, min(batch_size, 100))
        self.poll_interval_seconds = max(0.2, poll_interval_seconds)
        self.lease_seconds = max(1, lease_seconds)
        self.worker_id = worker_id or _default_worker_id()
        self.purge_interval_seconds = max(1.0, purge_interval_seconds)
        self._last_purge = 0.0
        self.webhook_dispatcher = webhook_dispatcher
        self.role = role
        self.queues = queues_for_role(role)
        self.job_timeout_seconds = max(1, job_timeout_seconds)
        self.reap_interval_seconds = max(1.0, reap_interval_seconds)
        self._last_reap = 0.0

    def run_once(self) -> list[BackgroundJob]:
        session = self.session_factory()
        try:
            store = self.store_factory(session)
            try:
                dispatched = store.dispatch_all_stage_execution_tasks()
            except Exception:
                session.rollback()
                dispatched = []
                logger.exception(
                    "worker %s failed while dispatching stage execution jobs",
                    self.worker_id,
                )
            if dispatched:
                logger.info(
                    "worker %s dispatched %s stage execution job(s)",
                    self.worker_id,
                    len(dispatched),
                )
            self._reap_stalled_jobs(store, session)
            jobs = store.claim_processable_background_jobs(
                worker_id=self.worker_id,
                limit=self.batch_size,
                lease_seconds=self.lease_seconds,
                queues=self.queues,
            )
            processed: list[BackgroundJob] = []
            for job in jobs:
                logger.info(
                    "worker %s processing background job %s (%s)",
                    self.worker_id,
                    job.job_id,
                    job.status,
                )
                started = time.perf_counter()
                result = store.process_background_job(job.job_id, worker_id=self.worker_id)
                WORKER_JOB_DURATION.observe(time.perf_counter() - started)
                if result is not None:
                    WORKER_JOBS_TOTAL.labels(
                        outcome=OUTCOME_FOR_STATUS.get(result.status.value, "other")
                    ).inc()
                    processed.append(result)
                    logger.info(
                        "worker %s processed background job %s -> %s (%s%%)",
                        self.worker_id,
                        result.job_id,
                        result.status,
                        result.progress,
                    )
            self._dispatch_webhooks(store, session)
            self._housekeeping(store, session)
            return processed
        finally:
            session.close()

    def _reap_stalled_jobs(self, store: DatabaseStore, session: Session) -> None:
        """Name the stalls: fail jobs that ran past the timeout (V8.1).

        Rate-limited like housekeeping — a reaper pass is a cheap indexed
        scan, but there is no reason to run it every loop. Best effort: a
        reaper failure must never delay normal job processing.
        """
        moment = time.monotonic()
        if moment - self._last_reap < self.reap_interval_seconds:
            return
        self._last_reap = moment
        reap = getattr(store, "reap_stalled_background_jobs", None)
        if reap is None:  # a test double that does not model the reaper
            return
        try:
            reaped = reap(timeout_seconds=self.job_timeout_seconds)
        except Exception:
            session.rollback()
            logger.exception("worker %s failed while reaping stalled jobs", self.worker_id)
            return
        if reaped:
            WORKER_JOBS_TOTAL.labels(outcome="reaped").inc(len(reaped))
            logger.warning(
                "worker %s reaped %s stalled background job(s)",
                self.worker_id,
                len(reaped),
            )

    def _dispatch_webhooks(self, store: DatabaseStore, session: Session) -> None:
        """Drain due webhook deliveries.

        Runs after job processing so an event published by a job in this same
        pass can be delivered without waiting a full poll interval. Best effort:
        a dead endpoint must never delay job processing, which is why everything
        here is wrapped and the transport timeout does the bounding.
        """
        if self.webhook_dispatcher is None:
            return
        try:
            settled = self.webhook_dispatcher.dispatch(
                store,
                worker_id=self.worker_id,
                limit=max(1, int(getattr(settings, "webhook_batch_size", 10))),
            )
        except Exception:
            session.rollback()
            logger.exception(
                "worker %s failed while dispatching webhook deliveries",
                self.worker_id,
            )
            return
        if settled:
            logger.info(
                "worker %s settled %s webhook delivery(ies)",
                self.worker_id,
                len(settled),
            )

    def _housekeeping(self, store: DatabaseStore, session: Session) -> None:
        """Drop bookkeeping that has aged out.

        Idempotency claims are the reason this exists: the SDK stamps a key on
        every mutating call, so those rows accrue at request rate and nothing else
        would ever remove them. Best effort and rate-limited — a failure here must
        never take down job processing, and a worker that just purged should not
        do it again on the next loop.
        """
        moment = time.monotonic()
        if moment - self._last_purge < self.purge_interval_seconds:
            return
        self._last_purge = moment
        purge = getattr(store, "purge_expired_idempotency_keys", None)
        if purge is None:  # a test double that does not model the table
            return
        try:
            removed = purge()
        except Exception:
            session.rollback()
            logger.exception("worker %s failed while purging expired keys", self.worker_id)
            return
        if removed:
            logger.info(
                "worker %s purged %s expired idempotency key(s)",
                self.worker_id,
                removed,
            )

    def run_forever(self, *, stop_event: Event | None = None) -> None:
        stop_event = stop_event or Event()
        logger.info(
            (
                "background worker %s started with role=%s queues=%s "
                "batch_size=%s poll_interval_seconds=%s lease_seconds=%s "
                "job_timeout_seconds=%s"
            ),
            self.worker_id,
            self.role,
            self.queues or "all",
            self.batch_size,
            self.poll_interval_seconds,
            self.lease_seconds,
            self.job_timeout_seconds,
        )
        while not stop_event.is_set():
            processed = self.run_once()
            if _should_wait_after_batch(processed):
                stop_event.wait(self.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Script2Video background job worker.")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
    parser.add_argument(
        "--role",
        choices=sorted(ROLE_QUEUES),
        default=os.getenv("WORKER_ROLE", DEFAULT_ROLE),
        help="Which job queues this worker claims (V8.1 worker scaling).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("WORKER_BATCH_SIZE", "5")),
        help="Maximum number of jobs to process per loop.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "5")),
        help="Seconds to wait between idle loops.",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=int(os.getenv("WORKER_LEASE_SECONDS", "300")),
        help="Seconds before another worker can reclaim a stuck job.",
    )
    parser.add_argument(
        "--worker-id",
        default=os.getenv("WORKER_ID"),
        help="Stable worker id. Defaults to hostname and process id.",
    )
    parser.add_argument(
        "--purge-interval",
        type=float,
        default=float(os.getenv("WORKER_PURGE_INTERVAL_SECONDS", "300")),
        help="Seconds between housekeeping sweeps (expired idempotency keys).",
    )
    parser.add_argument(
        "--job-timeout-seconds",
        type=int,
        default=int(os.getenv("WORKER_JOB_TIMEOUT_SECONDS", "1800")),
        help="Seconds a job may stay running before the reaper fails it.",
    )
    parser.add_argument(
        "--reap-interval",
        type=float,
        default=float(os.getenv("WORKER_REAP_INTERVAL_SECONDS", "60")),
        help="Seconds between stalled-job reaper sweeps.",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.getenv("WORKER_METRICS_PORT", "0")),
        help="Serve Prometheus worker metrics on this port (0 disables, V8.2).",
    )
    args = parser.parse_args()

    configure_logging(
        format=os.getenv("WORKER_LOG_FORMAT", "text"),
        level=os.getenv("WORKER_LOG_LEVEL", "INFO"),
    )

    if args.metrics_port > 0:
        # The exposition server is a plain daemon thread; starting it before
        # the loop means the first scrape already answers even before any job
        # has run.
        from prometheus_client import start_http_server

        start_http_server(args.metrics_port, registry=REGISTRY)
        logger.info("worker metrics served on port %s", args.metrics_port)

    worker = BackgroundJobWorker(
        batch_size=args.batch_size,
        poll_interval_seconds=args.poll_interval,
        lease_seconds=args.lease_seconds,
        worker_id=args.worker_id,
        purge_interval_seconds=args.purge_interval,
        webhook_dispatcher=_build_webhook_dispatcher(),
        role=args.role,
        job_timeout_seconds=args.job_timeout_seconds,
        reap_interval_seconds=args.reap_interval,
    )
    if args.once:
        processed = worker.run_once()
        logger.info("processed %s background job(s)", len(processed))
        return

    stop_event = Event()

    def stop(_signum: int, _frame: object) -> None:
        logger.info("background worker stopping")
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.run_forever(stop_event=stop_event)


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _build_webhook_dispatcher() -> WebhookDispatcher | None:
    """The production dispatcher, tuned by settings; ``None`` disables the loop."""
    if not settings.webhook_dispatch_enabled:
        return None
    return WebhookDispatcher(
        timeout=settings.webhook_request_timeout_seconds,
        lease_seconds=settings.webhook_lease_seconds,
        backoff_base_seconds=settings.webhook_backoff_base_seconds,
        backoff_cap_seconds=settings.webhook_backoff_cap_seconds,
        disable_after_consecutive_failures=settings.webhook_disable_after_failures,
    )


def _should_wait_after_batch(processed: list[BackgroundJob]) -> bool:
    if not processed:
        return True
    return all(job.status == BackgroundJobStatus.WAITING_PROVIDER for job in processed)


if __name__ == "__main__":
    main()
