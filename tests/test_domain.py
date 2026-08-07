from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eve_courier_optimizer.domain import (
    ActiveShipment,
    GateEvidence,
    GateThreatEvent,
    PlanningConstraints,
    PublicCourierContract,
    RoutableContract,
    SecurityBand,
    SecurityPolicy,
    SystemKillActivity,
    ThreatCategory,
    TravelTimeModel,
    cargo_capacity_to_units,
    cargo_volume_to_units,
    isk_to_units,
    isk_units_to_decimal,
    parse_esi_datetime,
    parse_human_isk,
    security_band,
    volume_units_to_decimal,
)


def test_esi_float_residue_is_conservatively_normalized() -> None:
    assert cargo_volume_to_units(Decimal("59347.244999999995")) == 59_347_245
    assert cargo_capacity_to_units(Decimal("59347.245999")) == 59_347_245


def test_isk_uses_centi_isk_integer_units() -> None:
    assert isk_to_units("100000000.0") == 10_000_000_000
    assert isk_to_units("1.235") == 124
    assert isk_units_to_decimal(124) == Decimal("1.24")
    assert volume_units_to_decimal(1_234) == Decimal("1.234")


@pytest.mark.parametrize(
    ("text", "unit", "expected"),
    [
        ("10M", "auto", Decimal("10000000")),
        ("4b", "auto", Decimal("4000000000")),
        ("3k ISK", "auto", Decimal("3000")),
        ("1,250M", "auto", Decimal("1250000000")),
        ("1.5", "b", Decimal("1500000000.0")),
        ("2M", "b", Decimal("2000000")),
        ("0", "isk", Decimal("0")),
    ],
)
def test_human_isk_parser_supports_suffixes_and_unit_selector(
    text: str,
    unit: str,
    expected: Decimal,
) -> None:
    assert parse_human_isk(text, unit=unit) == expected


@pytest.mark.parametrize(
    ("value", "unit"),
    [("nope", "auto"), ("-1", "auto"), ("10T", "auto"), ("1", "quadrillion")],
)
def test_human_isk_parser_rejects_ambiguous_values(value: str, unit: str) -> None:
    with pytest.raises(ValueError):
        parse_human_isk(value, unit=unit)


def test_security_bands_and_policy_rejection_provenance() -> None:
    assert security_band(0.45) is SecurityBand.HIGH
    assert security_band(0.449) is SecurityBand.LOW
    assert security_band(0.01) is SecurityBand.LOW
    assert security_band(0.0) is SecurityBand.NULL
    assert security_band(-1.0) is SecurityBand.NULL

    policy = SecurityPolicy(
        minimum_security=None,
        avoided_system_ids=frozenset({10}),
        allowed_bands=frozenset({SecurityBand.HIGH, SecurityBand.NULL}),
        gank_avoided_system_ids=frozenset({20}),
        gank_ship_kill_threshold=7,
        gank_activity_fetched_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert policy.rejection_reason(10, 1.0) == "manual_avoid_policy"
    assert policy.rejection_reason(20, 1.0) == "gank_activity_policy"
    assert policy.rejection_reason(30, 0.2) == "security_policy"
    assert policy.rejection_reason(30, -0.2) is None
    assert policy.permits(30, 0.9)


def test_security_policy_and_system_activity_validate_proof_inputs() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SecurityPolicy(allowed_bands=frozenset())
    with pytest.raises(ValueError, match="threshold"):
        SecurityPolicy(gank_ship_kill_threshold=0)
    with pytest.raises(ValueError, match="timestamp"):
        SecurityPolicy(gank_ship_kill_threshold=1)
    with pytest.raises(ValueError, match="timezone"):
        SecurityPolicy(
            gank_ship_kill_threshold=1,
            gank_activity_fetched_at=datetime(2026, 8, 5),
        )
    with pytest.raises(ValueError, match="threshold"):
        SecurityPolicy(gank_avoided_system_ids=frozenset({1}))
    with pytest.raises(ValueError, match="positive"):
        SystemKillActivity(0, 0, 0, 0)
    with pytest.raises(ValueError, match="cannot be negative"):
        SystemKillActivity(1, -1, 0, 0)


def test_gate_threat_policy_validates_and_reports_its_exact_boundary() -> None:
    observed_at = datetime(2026, 8, 5, tzinfo=UTC)
    policy = SecurityPolicy(
        threat_avoided_system_ids=frozenset({20}),
        threat_categories=frozenset({ThreatCategory.SMARTBOMB}),
        threat_min_events=2,
        threat_intel_fetched_at=observed_at,
        threat_window_seconds=86_400,
        threat_gate_radius_m=250_000,
        threat_coverage_region_ids=frozenset({10}),
        threat_incomplete_region_ids=frozenset({20}),
    )
    assert policy.rejection_reason(20, 1.0) == "gate_threat_policy"

    invalid_policies: tuple[dict[str, object], ...] = (
        {"threat_min_events": 0},
        {"threat_intel_fetched_at": datetime(2026, 8, 5)},
        {"threat_window_seconds": 0},
        {"threat_gate_radius_m": -1},
        {"threat_categories": frozenset()},
    )
    for overrides in invalid_policies:
        values = {
            "threat_avoided_system_ids": frozenset({20}),
            "threat_categories": frozenset({ThreatCategory.SMARTBOMB}),
            "threat_min_events": 1,
            "threat_intel_fetched_at": observed_at,
            "threat_window_seconds": 86_400,
            "threat_gate_radius_m": 250_000,
        }
        values.update(overrides)
        with pytest.raises(ValueError):
            SecurityPolicy(**values)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="player attacker"):
        GateThreatEvent(
            killmail_id=1,
            occurred_at=observed_at,
            system_id=1,
            region_id=10,
            gate_id=500,
            distance_to_gate_m=0,
            evidence=GateEvidence.ZKILL_LOCATION,
            categories=frozenset({ThreatCategory.ANY_GATE_PVP}),
            victim_ship_type_id=1,
            player_attacker_count=0,
        )


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity"])
def test_invalid_numeric_units_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        cargo_volume_to_units(value)
    with pytest.raises(ValueError):
        cargo_capacity_to_units(value)
    with pytest.raises(ValueError):
        isk_to_units(value)


def test_esi_datetime_requires_timezone_and_normalizes_to_utc() -> None:
    assert parse_esi_datetime("2026-08-05T12:30:00+02:00") == datetime(
        2026, 8, 5, 10, 30, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="timezone"):
        parse_esi_datetime("2026-08-05T12:30:00")


def _contract(**overrides: object) -> PublicCourierContract:
    values: dict[str, object] = {
        "contract_id": 1,
        "origin_location_id": 10,
        "destination_location_id": 20,
        "volume_units": 1_000,
        "collateral_units": 2_000,
        "reward_units": 3_000,
        "date_expired": datetime(2026, 8, 6, tzinfo=UTC),
        "days_to_complete": 1,
    }
    values.update(overrides)
    return PublicCourierContract(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract_id": 0},
        {"origin_location_id": 0},
        {"destination_location_id": 0},
        {"volume_units": -1},
        {"collateral_units": -1},
        {"reward_units": -1},
        {"days_to_complete": 0},
        {"date_expired": datetime(2026, 8, 6)},
    ],
)
def test_contract_rejects_invalid_invariants(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _contract(**overrides)


def test_routable_and_active_shipments_reject_invalid_coordinates() -> None:
    contract = _contract()
    with pytest.raises(ValueError, match="system IDs"):
        RoutableContract(contract, 0, 2)

    routable = RoutableContract(contract, 1, 2)
    with pytest.raises(ValueError, match="timezone"):
        ActiveShipment(routable, datetime(2026, 8, 6))


@pytest.mark.parametrize(
    ("jump_seconds", "service_seconds"),
    [(0, 0), (60, -1)],
)
def test_travel_time_model_rejects_invalid_values(
    jump_seconds: int, service_seconds: int
) -> None:
    with pytest.raises(ValueError):
        TravelTimeModel(jump_seconds, service_seconds)


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_system_id": 0},
        {"cargo_capacity_units": -1},
        {"collateral_budget_units": -1},
        {"horizon_seconds": -1},
        {"snapshot_time": datetime(2026, 8, 5)},
    ],
)
def test_planning_constraints_reject_invalid_values(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "start_system_id": 1,
        "cargo_capacity_units": 1,
        "collateral_budget_units": 1,
        "horizon_seconds": 1,
        "snapshot_time": datetime(2026, 8, 5, tzinfo=UTC),
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        PlanningConstraints(**values)  # type: ignore[arg-type]


def test_route_shape_constraints_validate_and_default_to_loop() -> None:
    base = PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=0,
        collateral_budget_units=0,
        horizon_seconds=60,
        snapshot_time=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert base.return_to_start
    assert base.terminal_system_id == 1

    open_route = PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=0,
        collateral_budget_units=0,
        horizon_seconds=60,
        snapshot_time=datetime(2026, 8, 5, tzinfo=UTC),
        return_to_start=False,
        finish_system_id=2,
        max_simultaneous_contracts=0,
    )
    assert open_route.terminal_system_id == 2

    with pytest.raises(ValueError, match="loop route cannot finish"):
        PlanningConstraints(
            start_system_id=1,
            cargo_capacity_units=0,
            collateral_budget_units=0,
            horizon_seconds=60,
            snapshot_time=datetime(2026, 8, 5, tzinfo=UTC),
            finish_system_id=2,
        )
    with pytest.raises(ValueError, match="max_simultaneous_contracts"):
        PlanningConstraints(
            start_system_id=1,
            cargo_capacity_units=0,
            collateral_budget_units=0,
            horizon_seconds=60,
            snapshot_time=datetime(2026, 8, 5, tzinfo=UTC),
            max_simultaneous_contracts=-1,
        )
