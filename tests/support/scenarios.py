from __future__ import annotations

from datetime import datetime, timedelta

from eve_courier_optimizer.domain import (
    CollateralMode,
    ContractSnapshot,
    PlanningConstraints,
    PublicCourierContract,
    SecurityPolicy,
    TravelTimeModel,
)
from eve_courier_optimizer.routing.universe import Region, SdeMetadata, SolarSystem, UniverseGraph


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


def make_tiny_graph() -> UniverseGraph:
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


def reward_constraints(
    now: datetime,
    *,
    horizon: int = 100,
    collateral: int = 1_000,
    mode: CollateralMode = CollateralMode.LOCKED,
) -> PlanningConstraints:
    return PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=100,
        collateral_budget_units=collateral,
        horizon_seconds=horizon,
        snapshot_time=now,
        collateral_mode=mode,
        travel=TravelTimeModel(seconds_per_jump=10, service_seconds=1),
        security=SecurityPolicy(0.45),
    )


def optimizer_constraints(
    now: datetime,
    *,
    cargo: int = 20,
    collateral: int = 200,
    mode: CollateralMode = CollateralMode.LOCKED,
    horizon: int = 1_000,
) -> PlanningConstraints:
    return PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=cargo,
        collateral_budget_units=collateral,
        horizon_seconds=horizon,
        snapshot_time=now,
        collateral_mode=mode,
        travel=TravelTimeModel(10, 1),
        security=SecurityPolicy(0.45),
    )


def trip_constraints(now: datetime, mode: CollateralMode) -> PlanningConstraints:
    return PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=20,
        collateral_budget_units=200,
        horizon_seconds=3_600,
        snapshot_time=now,
        collateral_mode=mode,
        travel=TravelTimeModel(10, 1),
        security=SecurityPolicy(0.45),
    )
