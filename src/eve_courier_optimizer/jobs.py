"""One cancellable background operation, isolated from the durable application state."""

from __future__ import annotations

import multiprocessing
import tempfile
import time
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .snapshot import write_snapshot

if TYPE_CHECKING:
    from .webapp import LocalWebApplication


def _run_job(
    app: LocalWebApplication,
    operation: str,
    body: dict[str, Any],
    workspace: str,
    connection: Connection,
) -> None:
    """Work on a private copy; only the parent can publish completed artifacts."""

    app.workspace = Path(workspace)
    app.snapshot_path = app.workspace / "snapshot.json"
    app.plan_path = app.workspace / "plan.json"
    app.execution_path = app.workspace / "execution.json"
    app.service.progress = lambda message: connection.send(("progress", message))
    if app.zkill is not None:
        # The previous worker may have sent a request just before cancellation. Treat worker
        # startup as the last request so the configured spacing also holds across jobs.
        app.zkill._last_network_request_epoch = app.zkill.now()
    try:
        action = {"scan": app.scan, "rank": app.rank, "solve": app.solve, "replan": app.replan}[
            operation
        ]
        payload = action(body)
        connection.send(("complete", (payload, app.snapshot, app.prepared, app.result)))
    except Exception as error:
        connection.send(("failed", str(error)))
    finally:
        connection.close()


class BackgroundJobs:
    """Serialize mutations while keeping HTTP status and cancellation responsive.

    Uses spawn on every platform: workers receive independent copies of graph/domain state.
    Cancellation terminates the whole worker, including solver threads and pending network waits.
    A cancelled or failed worker never publishes a partial snapshot or plan.
    """

    def __init__(self, app: LocalWebApplication) -> None:
        self.app = app
        self._job: dict[str, Any] | None = None
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._workspace: tempfile.TemporaryDirectory[str] | None = None
        self._started = 0.0

    def _cleanup(self) -> None:
        if self._process is not None:
            self._process.join(timeout=0.2)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=1)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=1)
            self._process.close()
            self._process = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None

    def _poll(self) -> None:
        if self._job is None or self._job["status"] != "running":
            return
        assert self._connection is not None and self._process is not None
        self._job["elapsed_seconds"] = time.monotonic() - self._started
        try:
            while self._connection.poll():
                kind, value = self._connection.recv()
                if kind == "progress":
                    self._job["progress"] = value
                    continue
                if kind == "complete":
                    payload, snapshot, prepared, result = value
                    # Publishing is synchronous with HTTP handling. No other mutation can run
                    # between these state updates. Invalidate old plans before replacing a scan.
                    operation = self._job["operation"]
                    if operation == "scan":
                        self.app._invalidate_plan()
                    if operation in {"scan", "replan"} and snapshot is not None:
                        write_snapshot(self.app.snapshot_path, snapshot)
                        self.app.snapshot = snapshot
                    if operation in {"solve", "replan"}:
                        self.app._store_plan(prepared, result)
                    self._job.update(status="completed", progress="Complete", result=payload)
                else:
                    self._job.update(status="failed", error=value)
                self._cleanup()
                return
            if not self._process.is_alive():
                # Check for a final message once more after observing worker exit.
                if self._connection.poll():
                    self._poll()
                    return
                raise RuntimeError("background worker exited without a result")
        except (EOFError, OSError, RuntimeError, ValueError) as error:
            self._job.update(status="failed", error=str(error) or "background worker disconnected")
            self._cleanup()

    def status(self, job_id: str | None = None) -> dict[str, Any] | None:
        self._poll()
        if job_id is not None and (self._job is None or self._job["id"] != job_id):
            raise ValueError("unknown background job")
        return dict(self._job) if self._job else None

    @property
    def running(self) -> bool:
        job = self.status()
        return job is not None and job["status"] == "running"

    def start(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.running:
            raise ValueError("a background job is already running")
        if operation not in {"scan", "rank", "solve", "replan"}:
            raise ValueError("unknown background operation")
        if self.app.execution is not None and operation != "replan":
            raise ValueError("an execution session already exists; use Replan")
        self._workspace = tempfile.TemporaryDirectory(prefix="eve-courier-job-")
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        self._connection = receiving
        self._process = context.Process(
            target=_run_job,
            args=(self.app, operation, body, self._workspace.name, sending),
            daemon=True,
        )
        self._started = time.monotonic()
        self._job = {
            "id": uuid4().hex,
            "operation": operation,
            "status": "running",
            "progress": "Starting",
            "elapsed_seconds": 0.0,
        }
        try:
            self._process.start()
        except Exception:
            # An unstarted process cannot be joined or closed as a running worker.
            self._process = None
            self._job = None
            self._cleanup()
            raise
        finally:
            sending.close()
        return dict(self._job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.status(job_id)
        assert job is not None and self._job is not None
        if job["status"] == "running":
            assert self._process is not None
            self._process.terminate()
            self._job.update(status="cancelled", progress="Cancelled")
            self._cleanup()
        return dict(self._job)

    def close(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
        if self._job is not None and self._job["status"] == "running":
            self._job.update(status="cancelled", progress="Server closed")
        self._cleanup()
