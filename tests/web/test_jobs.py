from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from eve_courier_optimizer.application.plan_contract import validate_contract
from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.eve.http import HttpResponse
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.background_jobs import BackgroundJobs
from eve_courier_optimizer.web.contracts import JobPayload, PlanResponse, RankResponse
from eve_courier_optimizer.web.server import create_http_server
from eve_courier_optimizer.web.workspace import PlanningWorkspace
from tests.support.assertions import present
from tests.support.clock import MutableClock
from tests.support.web import (
    CourierTransport,
    SlowTransport,
    planning_payload,
    post_json,
    proposal_input,
)


class ActivityTimeoutTransport(CourierTransport):
    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        if url.endswith("/universe/system_kills/"):
            raise TimeoutError("activity unavailable")
        return super().get(url, headers, timeout_seconds)


@pytest.fixture
def job_clock(now: datetime) -> MutableClock:
    return MutableClock(now)


@pytest.fixture
def job_app(
    tiny_graph: UniverseGraph, tmp_path: Path, job_clock: MutableClock
) -> PlanningWorkspace:
    return PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(job_clock.value)),
        tmp_path,
        clock=job_clock,
    )


@pytest.fixture
def jobs(job_app: PlanningWorkspace) -> Iterator[BackgroundJobs]:
    manager = BackgroundJobs(job_app)
    yield manager
    manager.close()


def finish(jobs: BackgroundJobs, job: JobPayload) -> JobPayload:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        current = jobs.status(job["id"])
        assert current is not None
        if current["status"] != "running":
            return current
        time.sleep(0.03)
    raise AssertionError("background job did not finish")


def test_background_scan_rank_solve_replan_publish_complete_results(
    jobs: BackgroundJobs,
    job_app: PlanningWorkspace,
    job_clock: MutableClock,
) -> None:
    assert jobs.status() is None
    scan = finish(jobs, jobs.start("scan", {"regions": [10]}))
    assert scan["status"] == "completed", scan
    assert job_app.snapshot is not None
    assert job_app.snapshot.fetched_at == job_clock.value
    assert job_app.artifact("snapshot.json") is not None
    ranked = finish(jobs, jobs.start("rank", planning_payload()))
    assert ranked["status"] == "completed", ranked
    validate_contract(ranked["result"], RankResponse, "rank result")
    assert cast(RankResponse, ranked["result"])["items"][0]["contract_id"] == 9001
    solved = finish(jobs, jobs.start("solve", planning_payload()))
    assert solved["status"] == "completed", solved
    assert job_app.status()["plan_armable"]
    assert job_app.artifact("plan.json") is not None
    job_app.start_execution(proposal_input(job_app, {"confirm_locked_acceptance": True}))
    with pytest.raises(ValueError, match="use Replan"):
        jobs.start("scan", {"regions": [10]})
    job_clock.value += timedelta(minutes=1)
    replanned = finish(jobs, jobs.start("replan", {"refresh": True}))
    assert replanned["status"] == "completed", replanned
    validate_contract(replanned["result"], PlanResponse, "replan result")
    assert present(cast(PlanResponse, replanned["result"])["execution"])["active_count"] == 1
    assert job_app.snapshot is not None and job_app.snapshot.fetched_at == job_clock.value
    assert jobs.cancel(replanned["id"])["status"] == "completed"


def test_failed_cancelled_and_closed_jobs_preserve_saved_artifacts(
    jobs: BackgroundJobs,
    job_app: PlanningWorkspace,
) -> None:
    with pytest.raises(ValueError, match="unknown"):
        jobs.status("missing")
    with pytest.raises(ValueError, match="unknown"):
        jobs.start("invalid", {})
    failed = finish(jobs, jobs.start("solve", planning_payload()))
    assert failed["status"] == "failed"
    assert "scan at least" in failed["error"]
    job_app.scan({"regions": [10]})
    job_app.solve(planning_payload())
    saved = {job_app.store.path: job_app.store.path.read_bytes()}
    job_app.esi.transport = SlowTransport(job_app.clock())
    running = jobs.start("scan", {"regions": [10]})
    with pytest.raises(ValueError, match="already running"):
        jobs.start("rank", planning_payload())
    assert jobs.cancel(running["id"])["status"] == "cancelled"
    assert {p: p.read_bytes() for p in saved} == saved
    jobs.start("scan", {"regions": [10]})
    jobs.close()
    assert {p: p.read_bytes() for p in saved} == saved


def test_optional_activity_timeout_keeps_successful_contract_scan(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    client = EsiClient(transport=ActivityTimeoutTransport(now), max_retries=0)
    app = PlanningWorkspace(tiny_graph, client, tmp_path, clock=MutableClock(now))
    scanned = app.scan({"regions": [10]})
    assert present(scanned["snapshot"])["contracts"] == 1
    assert app.snapshot is not None and app.snapshot.system_kills_fetched_at is None


def test_http_jobs_remain_responsive_and_reject_concurrent_mutations(
    job_app: PlanningWorkspace,
) -> None:
    job_app.esi.transport = SlowTransport(job_app.clock())
    server = create_http_server(job_app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        job = post_json(base + "/api/jobs", {"operation": "scan", "input": {"regions": [10]}})[
            "job"
        ]
        with urlopen(base + "/api/status", timeout=2) as response:
            assert json.load(response)["job"]["status"] == "running"
        with pytest.raises(HTTPError) as conflict:
            post_json(base + "/api/execution/reset", {})
        assert conflict.value.code == 409
        with urlopen(base + "/api/jobs/" + job["id"], timeout=2) as response:
            assert json.load(response)["job"]["id"] == job["id"]
        assert post_json(base + f"/api/jobs/{job['id']}/cancel", {})["job"]["status"] == "cancelled"
        with pytest.raises(HTTPError) as missing:
            urlopen(base + "/api/jobs/missing", timeout=2)
        assert missing.value.code == 404
        with pytest.raises(HTTPError) as invalid:
            post_json(base + "/api/jobs", {"operation": "scan", "input": []})
        assert invalid.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
