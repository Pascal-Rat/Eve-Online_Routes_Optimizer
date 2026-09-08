"""Spawn one cancellable worker; publish its complete result only in the parent session."""

from __future__ import annotations

import logging
import multiprocessing
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal
from uuid import uuid4

from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.planner import CourierPlanner
from eve_courier_optimizer.domain import ContractSnapshot
from eve_courier_optimizer.eve.esi import EsiClient, EsiError
from eve_courier_optimizer.eve.zkill import ZkillClient, ZkillError
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.contracts import JobFields, JobPayload, OperationResponse
from eve_courier_optimizer.web.operations import Operation, OperationResult, compute_operation
from eve_courier_optimizer.web.workspace import PlanningWorkspace


class JobStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Failed:
    message: str


@dataclass(frozen=True, slots=True)
class PublishedResult:
    result: OperationResponse


@dataclass(slots=True)
class Job:
    id: str
    operation: Operation
    progress: str = "Starting"
    elapsed_seconds: float = 0.0
    outcome: Literal[JobStatus.RUNNING, JobStatus.CANCELLED] | PublishedResult | Failed = (
        JobStatus.RUNNING
    )

    @property
    def status(self) -> JobStatus:
        if isinstance(self.outcome, PublishedResult):
            return JobStatus.COMPLETED
        if isinstance(self.outcome, Failed):
            return JobStatus.FAILED
        return self.outcome

    def to_dict(self) -> JobPayload:
        common: JobFields = {
            "id": self.id,
            "operation": self.operation.value,
            "progress": self.progress,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if isinstance(self.outcome, PublishedResult):
            return {**common, "status": "completed", "result": self.outcome.result}
        if isinstance(self.outcome, Failed):
            return {**common, "status": "failed", "error": self.outcome.message}
        if self.outcome is JobStatus.RUNNING:
            return {**common, "status": "running"}
        return {**common, "status": "cancelled"}


@dataclass(frozen=True, slots=True)
class JobInputs:
    graph: UniverseGraph
    esi: EsiClient
    zkill: ZkillClient | None
    clock: Callable[[], datetime]
    snapshot: ContractSnapshot | None
    trip: CourierTrip | None
    revision: int


@dataclass(frozen=True, slots=True)
class Progress:
    message: str


@dataclass(frozen=True, slots=True)
class Completed:
    result: OperationResult
    expected_revision: int


def _run_job(
    inputs: JobInputs,
    operation: Operation,
    body: dict[str, object],
    connection: Connection,
) -> None:
    try:

        def report_progress(message: str) -> None:
            connection.send(Progress(message))

        planner = CourierPlanner(
            inputs.graph, inputs.esi, inputs.zkill, progress=report_progress, clock=inputs.clock
        )
        if inputs.zkill is not None:
            # Cancellation can interrupt a request; preserve request spacing across workers.
            inputs.zkill.defer_next_request()
        result = compute_operation(
            operation,
            planner,
            body,
            observation=inputs.snapshot,
            trip=inputs.trip,
            at=inputs.clock(),
        )
        connection.send(Completed(result, inputs.revision))
    except Exception as error:
        # This is the process boundary: uncaught failures must reach the operator.
        if not isinstance(error, (ValueError, EsiError, ZkillError)):
            logging.getLogger(__name__).exception("Background %s failed", operation.value)
        connection.send(Failed(str(error)))
    finally:
        connection.close()


@dataclass(slots=True)
class _Worker:
    process: BaseProcess
    connection: Connection
    started: float

    def close(self) -> None:
        if self.process.pid is not None:
            self.process.join(timeout=0.2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1)
        self.process.close()
        self.connection.close()


class BackgroundJobs:
    """Single-writer publication with responsive status and whole-process cancellation."""

    def __init__(self, app: PlanningWorkspace) -> None:
        self.app = app
        self._job: Job | None = None
        self._worker: _Worker | None = None

    def _cleanup(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None

    def _poll(self) -> None:
        job, worker = self._job, self._worker
        if job is None or job.status is not JobStatus.RUNNING:
            return
        assert worker is not None
        job.elapsed_seconds = time.monotonic() - worker.started
        try:
            while worker.connection.poll():
                message = worker.connection.recv()
                if isinstance(message, Progress):
                    job.progress = message.message
                    continue
                if isinstance(message, Completed):
                    payload = self.app.publish(
                        message.result, expected_revision=message.expected_revision
                    )
                    job.outcome, job.progress = PublishedResult(payload), "Complete"
                elif isinstance(message, Failed):
                    job.outcome = message
                else:
                    raise RuntimeError("background worker returned an unknown message")
                self._cleanup()
                return
            if not worker.process.is_alive():
                if worker.connection.poll():
                    self._poll()
                    return
                raise RuntimeError("background worker exited without a result")
        except (EOFError, OSError, RuntimeError, ValueError) as error:
            job.outcome = Failed(str(error) or "background worker disconnected")
            self._cleanup()

    def status(self, job_id: str | None = None) -> JobPayload | None:
        self._poll()
        if job_id is not None and (self._job is None or self._job.id != job_id):
            raise ValueError("unknown background job")
        return self._job.to_dict() if self._job else None

    @property
    def running(self) -> bool:
        self._poll()
        return self._job is not None and self._job.status is JobStatus.RUNNING

    def start(self, operation: str, body: dict[str, object]) -> JobPayload:
        if self.running:
            raise ValueError("a background job is already running")
        try:
            requested = Operation(operation)
        except ValueError as error:
            raise ValueError("unknown background operation") from error
        if self.app.trip is not None and requested is not Operation.REPLAN:
            raise ValueError("an execution session already exists; use Replan")
        self.app.require_revision(body)
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        inputs = JobInputs(
            self.app.graph,
            self.app.esi,
            self.app.zkill,
            self.app.clock,
            self.app.observation,
            self.app.trip,
            self.app.revision,
        )
        process = context.Process(
            target=_run_job, args=(inputs, requested, body, sending), daemon=True
        )
        self._worker = _Worker(process, receiving, time.monotonic())
        self._job = Job(uuid4().hex, requested)
        try:
            process.start()
        except Exception:
            self._job = None
            self._cleanup()
            raise
        finally:
            sending.close()
        return self._job.to_dict()

    def cancel(self, job_id: str) -> JobPayload:
        self.status(job_id)
        assert self._job is not None
        if self._job.status is JobStatus.RUNNING:
            assert self._worker is not None
            self._worker.process.terminate()
            self._job.outcome, self._job.progress = JobStatus.CANCELLED, "Cancelled"
            self._cleanup()
        return self._job.to_dict()

    def close(self) -> None:
        if self._worker is not None and self._worker.process.is_alive():
            self._worker.process.terminate()
        if self._job is not None and self._job.status is JobStatus.RUNNING:
            self._job.outcome, self._job.progress = JobStatus.CANCELLED, "Server closed"
        self._cleanup()
