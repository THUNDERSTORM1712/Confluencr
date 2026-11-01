"""Independent background worker: `python -m app.workers.transaction_worker`."""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings, get_settings
from app.database.connection import dispose_engines
from app.database.session import session_scope
from app.logging_config import bind_context, clear_context, configure_logging, log_event
from app.services import job_service
from app.services.job_service import ClaimedJob
from app.workers import heartbeat, job_recovery

logger = logging.getLogger("app.worker")

# processor(job, abort_event) -> True if finished, False if aborted. Raise to signal failure.
Processor = Callable[[ClaimedJob, threading.Event], bool]


class TransactionWorker:
    def __init__(self, settings: Settings, worker_id: uuid.UUID | None = None, processor: Processor | None = None):
        self.settings = settings
        self.worker_id = worker_id or uuid.uuid4()
        self.hostname = os.environ.get("RENDER_INSTANCE_ID") or socket.gethostname()
        self.processor = processor or self._simulate_external_processing
        self.stop_event = threading.Event()
        self._active: dict[uuid.UUID, threading.Event] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=settings.worker_concurrency, thread_name_prefix="job")
        self._heartbeat: heartbeat.HeartbeatThread | None = None
        self._last_housekeeping = 0.0

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        heartbeat.register_worker(self.worker_id, self.hostname)
        self._heartbeat = heartbeat.HeartbeatThread(self.worker_id, self.settings.worker_heartbeat_interval_seconds)
        self._heartbeat.start()
        log_event(logger, logging.INFO, "worker_started", worker_id=self.worker_id, detail=self.hostname)

    def _start_with_retry(self) -> bool:
        """Register with retries so a database that is briefly unreachable at boot cannot kill the worker."""
        while not self.stop_event.is_set():
            try:
                self.start()
                return True
            except Exception as exc:
                log_event(logger, logging.ERROR, "worker_start_failed", error_type=type(exc).__name__)
                self.stop_event.wait(self.settings.worker_poll_interval_seconds * 2)
        return False

    def run(self) -> None:
        if not self._start_with_retry():
            self._executor.shutdown(wait=False)
            return
        try:
            while not self.stop_event.is_set():
                try:
                    self.poll_once()
                except Exception as exc:  # DB outage etc.: log and keep polling
                    log_event(logger, logging.ERROR, "worker_poll_failed",
                              worker_id=self.worker_id, error_type=type(exc).__name__)
                self.stop_event.wait(self.settings.worker_poll_interval_seconds)
        finally:
            self.shutdown()

    def request_stop(self) -> None:
        """Stop claiming new jobs and tell in-flight jobs to wind down (they are released, not completed)."""
        self.stop_event.set()
        with self._lock:
            for abort in self._active.values():
                abort.set()

    def shutdown(self) -> None:
        self.request_stop()
        self._executor.shutdown(wait=True)
        if self._heartbeat:
            self._heartbeat.stop()
        try:
            heartbeat.deregister_worker(self.worker_id)
        except Exception as exc:
            log_event(logger, logging.ERROR, "worker_deregister_failed", error_type=type(exc).__name__)
        log_event(logger, logging.INFO, "worker_stopped", worker_id=self.worker_id)

    # ---- polling ---------------------------------------------------------------------------
    def poll_once(self) -> int:
        """Housekeeping + claim as many jobs as there is free capacity. Returns jobs claimed."""
        self._housekeeping()
        with self._lock:
            capacity = self.settings.worker_concurrency - len(self._active)
        if capacity <= 0 or self.stop_event.is_set():
            return 0
        with session_scope("worker") as session:  # short transaction; commits the claim before processing
            jobs = job_service.claim_jobs(session, self.worker_id, capacity, self.settings.worker_lease_seconds)
        for job in jobs:
            abort = threading.Event()
            with self._lock:
                self._active[job.job_id] = abort
            self._executor.submit(self.process_job, job, abort)
        return len(jobs)

    def _housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_housekeeping < self.settings.worker_heartbeat_interval_seconds:
            return
        self._last_housekeeping = now
        with session_scope("worker") as session:
            job_recovery.fail_exhausted_jobs(session)
            job_recovery.mark_stale_workers(session, self.settings.worker_stale_after_seconds)

    # ---- job processing --------------------------------------------------------------------
    def _simulate_external_processing(self, job: ClaimedJob, abort: threading.Event) -> bool:
        """Stands in for external API calls; waits PROCESSING_DELAY_SECONDS without blocking other jobs."""
        return not abort.wait(self.settings.processing_delay_seconds)

    def _keep_lease(self, job: ClaimedJob, abort: threading.Event, done: threading.Event, lost: threading.Event) -> None:
        while not done.wait(self.settings.worker_heartbeat_interval_seconds):
            try:
                with session_scope("worker") as session:
                    renewed = job_service.renew_lease(session, job.job_id, job.lease_token, self.settings.worker_lease_seconds)
            except Exception as exc:
                log_event(logger, logging.WARNING, "job_lease_renew_error", error_type=type(exc).__name__)
                continue  # transient; the lease has slack (>= 2 heartbeat intervals)
            if not renewed:
                lost.set()
                abort.set()
                return

    def backoff_seconds(self, attempts: int) -> float:
        s = self.settings
        return min(s.retry_backoff_base_seconds * (2 ** max(attempts - 1, 0)), s.retry_backoff_max_seconds)

    def process_job(self, job: ClaimedJob, abort: threading.Event | None = None) -> None:
        abort = abort or threading.Event()
        with self._lock:
            self._active.setdefault(job.job_id, abort)
        clear_context()
        bind_context(worker_id=self.worker_id, job_id=job.job_id, transaction_id=job.transaction_id,
                     correlation_id=job.correlation_id)
        done, lost = threading.Event(), threading.Event()
        keeper = threading.Thread(target=self._keep_lease, args=(job, abort, done, lost), daemon=True)
        keeper.start()
        started = time.perf_counter()
        log_event(logger, logging.INFO, "job_processing_started", processing_status="PROCESSING", attempt=job.attempts)
        try:
            finished = self.processor(job, abort)
            done.set()
            if lost.is_set():
                log_event(logger, logging.WARNING, "job_lease_lost", reason="not completing; another worker owns the job")
            elif not finished:
                with session_scope("worker") as session:
                    job_service.release_job(session, job.job_id, job.lease_token)
                log_event(logger, logging.INFO, "job_released_on_shutdown")
            else:
                with session_scope("worker") as session:
                    ok = job_service.complete_job(session, job.job_id, job.lease_token)
                log_event(
                    logger, logging.INFO if ok else logging.WARNING,
                    "job_completed" if ok else "job_completion_rejected_stale_lease",
                    processing_status="PROCESSED" if ok else None,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
        except Exception as exc:
            done.set()
            self._handle_failure(job, exc)
        finally:
            done.set()
            with self._lock:
                self._active.pop(job.job_id, None)
            clear_context()

    def _handle_failure(self, job: ClaimedJob, exc: Exception) -> None:
        backoff = self.backoff_seconds(job.attempts)
        try:
            with session_scope("worker") as session:
                new_status = job_service.fail_job(
                    session, job.job_id, job.lease_token, f"{type(exc).__name__}: {exc}", backoff
                )
        except Exception as db_exc:  # lease will expire and the job is reclaimed
            log_event(logger, logging.ERROR, "job_failure_record_failed", error_type=type(db_exc).__name__)
            return
        log_event(
            logger, logging.ERROR if new_status == "FAILED" else logging.WARNING,
            "job_failed_permanently" if new_status == "FAILED" else "job_retry_scheduled",
            error_type=type(exc).__name__, attempt=job.attempts, detail=f"backoff_seconds={backoff}",
            processing_status="FAILED" if new_status == "FAILED" else "PROCESSING",
        )


def main() -> None:
    settings = get_settings()
    configure_logging(f"{settings.app_name}-worker", settings.app_env, settings.log_level, settings.log_format)
    worker = TransactionWorker(settings)

    def _signal(signum, _frame):
        log_event(logger, logging.INFO, "worker_shutdown_requested", detail=signal.Signals(signum).name)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    try:
        worker.run()
    finally:
        dispose_engines()


if __name__ == "__main__":
    main()
