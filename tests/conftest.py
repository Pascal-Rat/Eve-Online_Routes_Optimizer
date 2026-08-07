from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eve_courier_optimizer.domain import PublicCourierContract
from eve_courier_optimizer.sde import Region, SdeMetadata, SolarSystem, UniverseGraph
from eve_courier_optimizer.snapshot import ContractSnapshot


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def tiny_graph() -> UniverseGraph:
    systems = {
        1: SolarSystem(1, 10, "Alpha", 1.0),
        2: SolarSystem(2, 10, "Beta", 0.9),
        3: SolarSystem(3, 10, "Gamma", 0.8),
        4: SolarSystem(4, 10, "Low", 0.2),
        5: SolarSystem(5, 10, "Island", 0.9),
    }
    return UniverseGraph(
        systems=systems,
        adjacency={1: (2,), 2: (1, 3, 4), 3: (2,), 4: (2,), 5: ()},
        station_systems={101: 1, 102: 2, 103: 3, 104: 4, 105: 5},
        regions={10: Region(10, "Test Region")},
        metadata=SdeMetadata(1, "2026-08-05T11:00:00Z", "test://sde"),
    )


def make_contract(
    now: datetime,
    contract_id: int,
    origin_location_id: int,
    destination_location_id: int,
    *,
    volume: int = 10,
    collateral: int = 100,
    reward: int = 500,
    days: int = 1,
    expiry_hours: int = 24,
) -> PublicCourierContract:
    return PublicCourierContract(
        contract_id=contract_id,
        origin_location_id=origin_location_id,
        destination_location_id=destination_location_id,
        volume_units=volume,
        collateral_units=collateral,
        reward_units=reward,
        date_expired=now + timedelta(hours=expiry_hours),
        days_to_complete=days,
        date_issued=now - timedelta(hours=1),
    )


def make_snapshot(
    now: datetime,
    *contracts: PublicCourierContract,
    sde_build: int = 1,
) -> ContractSnapshot:
    return ContractSnapshot(now, "2026-08-05", sde_build, (10,), tuple(contracts))
