from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.server import create_http_server
from eve_courier_optimizer.web.workspace import PlanningWorkspace
from tests.support.clock import MutableClock
from tests.support.scenarios import make_tiny_graph
from tests.support.web import CourierTransport


@pytest.fixture
def tiny_graph() -> UniverseGraph:
    return make_tiny_graph()


@dataclass
class WebSession:
    app: PlanningWorkspace
    clock: MutableClock
    url: str


@pytest.fixture
def web_session(tiny_graph: UniverseGraph, tmp_path: Path) -> Iterator[WebSession]:
    clock = MutableClock(datetime(2026, 8, 5, 12, tzinfo=UTC))
    app = PlanningWorkspace(
        tiny_graph, EsiClient(transport=CourierTransport(clock.value)), tmp_path, clock=clock
    )
    server = create_http_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield WebSession(app, clock, f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
