from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.server import LocalHTTPServer
from eve_courier_optimizer.web.workspace import PlanningWorkspace
from tests.support.web import (
    CourierTransport,
    SlowTransport,
    planning_payload,
    post_json,
    proposal_input,
)


@pytest.fixture
def local_server(
    tiny_graph: UniverseGraph, tmp_path: Path
) -> Iterator[tuple[PlanningWorkspace, LocalHTTPServer, str]]:
    app = PlanningWorkspace(
        tiny_graph, EsiClient(transport=CourierTransport(datetime.now(UTC))), tmp_path
    )
    server = LocalHTTPServer(app, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield app, server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("partial", [False, True])
def test_idle_or_partial_client_does_not_block_status_and_cancel(
    local_server: tuple[PlanningWorkspace, LocalHTTPServer, str], partial: bool
) -> None:
    app, server, base = local_server
    app.esi.transport = SlowTransport(datetime.now(UTC))
    saved = app.store.path.read_bytes()
    job = post_json(f"{base}/api/jobs", {"operation": "scan", "input": {"regions": [10]}})["job"]
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as idle:
        if partial:
            idle.sendall(
                b"POST /api/scan HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 50\r\n\r\n{"
            )
        with urlopen(f"{base}/api/status", timeout=2) as response:
            assert json.load(response)["job"]["status"] == "running"
        cancelled = post_json(f"{base}/api/jobs/{job['id']}/cancel", {})
        assert cancelled["job"]["status"] == "cancelled"
    assert app.store.path.read_bytes() == saved


def test_concurrent_acceptance_applies_exactly_one_transition(
    local_server: tuple[PlanningWorkspace, LocalHTTPServer, str],
) -> None:
    app, _, base = local_server
    app.scan({"regions": [10]})
    app.solve(planning_payload())
    reviewed = proposal_input(app, {"confirm_locked_acceptance": True})
    revision = app.revision
    barrier = threading.Barrier(2)

    def accept() -> int:
        barrier.wait(timeout=2)
        try:
            post_json(f"{base}/api/execution/start", reviewed)
            return 200
        except HTTPError as error:
            return error.code

    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(accept) for _ in range(2)]
        assert sorted(future.result(timeout=5) for future in futures) == [200, 409]
    assert app.revision == revision + 1
    assert app.trip is not None and len(app.trip.active_shipments) == 1
