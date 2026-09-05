from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eve_courier_optimizer.esi import EsiClient
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.webapp import LocalWebApplication, create_http_server
from tests.conftest import tiny_graph as tiny_graph
from tests.test_live_recovery import MutableClock
from tests.test_webapp import CourierTransport


@dataclass
class WebSession:
    app: LocalWebApplication
    clock: MutableClock
    url: str


@pytest.fixture
def web_session(tiny_graph: UniverseGraph, tmp_path: Path) -> Iterator[WebSession]:
    clock = MutableClock(datetime.now(UTC))
    app = LocalWebApplication(
        tiny_graph, EsiClient(transport=CourierTransport(clock.value)), tmp_path, clock=clock
    )
    server = create_http_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield WebSession(app, clock, f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
