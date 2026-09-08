from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eve_courier_optimizer.routing.universe import UniverseGraph
from tests.support.scenarios import make_tiny_graph


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def tiny_graph() -> UniverseGraph:
    return make_tiny_graph()
