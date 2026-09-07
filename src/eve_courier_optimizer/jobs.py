"""Spawn one cancellable worker; publish its complete result only in the parent session."""

from __future__ import annotations

import logging
import multiprocessing
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any
from uuid import uuid4

from .esi import EsiClient, EsiError
from .execution import ExecutionState
from .sde import UniverseGraph
from .service import RoutePlan
from .session import PlanningSession
from .snapshot import ContractSnapshot, write_snapshot
from .threat_intel import ZkillClient, ZkillError


class Operation(StrEnum):
    SCAN = "scan"
    RANK = "rank"
    SOLVE = "solve"
    REPLAN = "replan"


class JobStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Job:
    id: str
    operation: Operation
    status: JobStatus = JobStatus.RUNNING
    progress: str = "Starting"
    elapsed_seconds: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "operation": self.operation.value,
            "status": self.status.value,
            "progress": self.progress,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if self.result is not None:
            payload["result"] = self.result
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class JobInputs:
    graph: UniverseGraph
    esi: EsiClient
    zkill: ZkillClient | None
    clock: Callable[[], datetime]
    snapshot: ContractSnapshot | None
    execution: ExecutionState | None


@dataclass(frozen=True, slots=True)
class Progress:
    message: str


@dataclass(frozen=True, slots=True)
class Completed:
    payload: dict[str, Any]
    snapshot: ContractSnapshot | None
    plan: RoutePlan | None


@dataclass(frozen=True, slots=True)
class Failed:
    message: str


def _run_job(
    inputs: JobInputs,
    operation: Operation,
    body: dict[str, object],
    workspace: str,
    connection: Connection,
) -> None:
    try:
        session = PlanningSession(
            inputs.graph, inputs.esi, Path(workspace), inputs.zkill, clock=inputs.clock
        )
        session.snapshot = inputs.snapshot
        session.execution = inputs.execution
        session.service.progress = lambda message: connection.send(Progress(message))
        if session.zkill is not None:
            # Cancellation can interrupt a request; preserve request spacing across workers.
            session.zkill.defer_next_request()
        action = {
            Operation.SCAN: session.scan,
            Operation.RANK: session.rank,
            Operation.SOLVE: session.solve,
            Operation.REPLAN: session.replan,
        }[operation]
        payload = action(body)
        connection.send(Completed(payload, session.snapshot, session.plan))
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
    workspace: tempfile.TemporaryDirectory[str]
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
        self.workspace.cleanup()


class BackgroundJobs:
    """Single-writer publication with responsive status and whole-process cancellation."""

    def __init__(self, app: PlanningSession) -> None:
        self.app = app
        self._job: Job | None = None
        self._worker: _Worker | None = None

    def _cleanup(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None

    def _publish(self, completed: Completed, operation: Operation) -> None:
        if operation is Operation.SCAN:
            self.app._invalidate_plan()
        if operation in {Operation.SCAN, Operation.REPLAN} and completed.snapshot is not None:
            write_snapshot(self.app.snapshot_path, completed.snapshot)
            self.app.snapshot = completed.snapshot
        if operation in {Operation.SOLVE, Operation.REPLAN}:
            if completed.plan is None:
                raise RuntimeError("completed planning job has no plan")
            self.app.store_plan(completed.plan)

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
                    self._publish(message, job.operation)
                    job.status, job.progress, job.result = (
                        JobStatus.COMPLETED,
                        "Complete",
                        message.payload,
                    )
                elif isinstance(message, Failed):
                    job.status, job.error = JobStatus.FAILED, message.message
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
            job.status, job.error = JobStatus.FAILED, str(error) or "background worker disconnected"
            self._cleanup()

    def status(self, job_id: str | None = None) -> dict[str, Any] | None:
        self._poll()
        if job_id is not None and (self._job is None or self._job.id != job_id):
            raise ValueError("unknown background job")
        return self._job.to_dict() if self._job else None

    @property
    def running(self) -> bool:
        self._poll()
        return self._job is not None and self._job.status is JobStatus.RUNNING

    def start(self, operation: str, body: dict[str, object]) -> dict[str, Any]:
        if self.running:
            raise ValueError("a background job is already running")
        try:
            requested = Operation(operation)
        except ValueError as error:
            raise ValueError("unknown background operation") from error
        if self.app.execution is not None and requested is not Operation.REPLAN:
            raise ValueError("an execution session already exists; use Replan")
        workspace = tempfile.TemporaryDirectory(prefix="eve-courier-job-")
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        inputs = JobInputs(
            self.app.graph,
            self.app.esi,
            self.app.zkill,
            self.app.clock,
            self.app.snapshot,
            self.app.execution,
        )
        process = context.Process(
            target=_run_job, args=(inputs, requested, body, workspace.name, sending), daemon=True
        )
        self._worker = _Worker(process, receiving, workspace, time.monotonic())
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

    def cancel(self, job_id: str) -> dict[str, Any]:
        self.status(job_id)
        assert self._job is not None
        if self._job.status is JobStatus.RUNNING:
            assert self._worker is not None
            self._worker.process.terminate()
            self._job.status, self._job.progress = JobStatus.CANCELLED, "Cancelled"
            self._cleanup()
        return self._job.to_dict()

    def close(self) -> None:
        if self._worker is not None and self._worker.process.is_alive():
            self._worker.process.terminate()
        if self._job is not None and self._job.status is JobStatus.RUNNING:
            self._job.status, self._job.progress = JobStatus.CANCELLED, "Server closed"
        self._cleanup()
