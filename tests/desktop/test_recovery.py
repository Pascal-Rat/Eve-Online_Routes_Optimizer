from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eve_courier_optimizer.application.execution import (
    constraints_for_replan,
    extend_execution_horizon,
    read_execution_state,
    record_delivery,
    record_pickup,
    record_route_system,
    write_execution_state,
)
from eve_courier_optimizer.application.planner import CourierPlanner
from eve_courier_optimizer.cli import main
from eve_courier_optimizer.desktop.session import PlanningSession
from eve_courier_optimizer.domain import CollateralMode
from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.eve.snapshot import write_snapshot
from eve_courier_optimizer.routing.universe import UniverseGraph
from tests.application.test_execution import constraints
from tests.conftest import make_contract, make_snapshot
from tests.desktop.test_server import CourierTransport, planning_payload


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def test_failed_reset_keeps_live_commitments(
    now: datetime, tiny_graph: UniverseGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = PlanningSession(tiny_graph, EsiClient(), tmp_path, clock=MutableClock(now))
    app.snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    app.solve(planning_payload())
    app.start_execution({"confirm_locked_acceptance": True})
    state = app.execution
    assert state is not None and state.active_shipments
    original_unlink = Path.unlink

    def fail(path: Path, missing_ok: bool = False) -> None:
        if path == app.execution_path:
            raise OSError("disk unavailable")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        app.reset_execution()
    assert app.execution == state
    assert read_execution_state(app.execution_path) == state


def test_locked_pickups_enforce_cargo_and_validate_persisted_state(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    snapshot = make_snapshot(
        now, make_contract(now, 1, 101, 102, volume=15), make_contract(now, 2, 101, 103, volume=15)
    )
    planner = CourierPlanner(tiny_graph, EsiClient())
    plan = planner.solve(snapshot, constraints(now, CollateralMode.LOCKED))
    state = planner.arm(plan, at=now)
    assert len(state.active_shipments) == 2
    picked = record_pickup(state, None, tiny_graph, 1, now)
    with pytest.raises(ValueError, match="cargo capacity"):
        record_pickup(picked, None, tiny_graph, 2, now)
    with pytest.raises(ValueError, match="cargo capacity"):
        replace(
            picked, active_shipments=tuple(replace(s, picked=True) for s in state.active_shipments)
        )
    with pytest.raises(ValueError, match="unique"):
        replace(state, active_shipments=(state.active_shipments[0],) * 2)
    with pytest.raises(ValueError, match="collateral"):
        replace(state, collateral_budget_units=0)
    with pytest.raises(ValueError):
        replace(state, current_system_id=0)
    with pytest.raises(ValueError):
        replace(state, cargo_capacity_units=-1)


def test_progress_outlives_horizon_and_extension_preserves_commitments(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    planner = CourierPlanner(tiny_graph, EsiClient())
    plan = planner.solve(
        snapshot,
        replace(constraints(now, CollateralMode.LOCKED), required_system_ids=frozenset({3})),
    )
    state = planner.arm(plan, at=now)
    later = now + timedelta(hours=2)
    picked = record_pickup(state, None, tiny_graph, 1, later)
    with pytest.raises(ValueError, match="extend the horizon"):
        constraints_for_replan(picked, snapshot)
    extended = extend_execution_horizon(picked, additional_seconds=3600, at=later)
    assert extended.active_shipments == picked.active_shipments
    assert extended.remaining_required_system_ids == picked.remaining_required_system_ids
    assert extended.terminal_system_id == picked.terminal_system_id
    assert constraints_for_replan(extended, snapshot).horizon_seconds == 3600
    delivered = record_delivery(picked, 1, later + timedelta(minutes=1))
    reached = record_route_system(delivered, 3, later + timedelta(minutes=2))
    path = tmp_path / "state.json"
    write_execution_state(path, reached)
    assert read_execution_state(path) == reached
    assert (
        main(
            [
                "extend",
                "--state",
                str(path),
                "--minutes",
                "30",
                "--at",
                reached.current_time.isoformat(),
                "--output",
                str(path),
            ]
        )
        == 0
    )
    assert read_execution_state(path).session_deadline == reached.current_time + timedelta(
        minutes=30
    )
    with pytest.raises(ValueError, match="deadline"):
        record_delivery(picked, 1, now + timedelta(days=2))
    with pytest.raises(ValueError, match="deadline"):
        record_pickup(state, None, tiny_graph, 1, now + timedelta(days=2))
    with pytest.raises(ValueError):
        extend_execution_horizon(picked, additional_seconds=0, at=later)
    with pytest.raises(ValueError):
        extend_execution_horizon(picked, additional_seconds=60, at=now)


def test_live_clock_filters_old_listings_and_revalidates_departure(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    clock = MutableClock(now)
    app = PlanningSession(tiny_graph, EsiClient(), tmp_path, clock=clock)
    contract = replace(make_contract(now, 1, 101, 102), date_expired=now + timedelta(minutes=1))
    app.snapshot = make_snapshot(now, contract)
    app.solve(planning_payload())
    assert app.plan is not None
    assert app.plan.prepared.problem.constraints.snapshot_time == now
    clock.value += timedelta(minutes=2)
    with pytest.raises(ValueError, match="listing has expired"):
        app.start_execution({"confirm_locked_acceptance": True})
    assert app.execution is None
    assert app.solve(planning_payload())["plan"]["summary"]["selected_contract_ids"] == []
    assert app.plan.prepared.problem.constraints.snapshot_time == clock.value


def test_replan_arming_rechecks_remaining_horizon(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    planner = CourierPlanner(tiny_graph, EsiClient())
    snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    plan = planner.solve(snapshot, constraints(now, CollateralMode.LOCKED))
    state = planner.arm(plan, at=now)
    plan = planner.replan(snapshot, state)
    with pytest.raises(ValueError, match="departure revalidation failed"):
        planner.arm(plan, at=state.session_deadline - timedelta(seconds=1), previous=state)
    armed = planner.arm(plan, at=now + timedelta(microseconds=123), previous=state)
    assert armed.session_deadline == state.session_deadline
    assert armed.active_shipments == state.active_shipments


def test_web_rank_is_strict_json_and_scan_invalidates_saved_plan(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    app = PlanningSession(
        tiny_graph, EsiClient(transport=CourierTransport(now)), tmp_path, clock=MutableClock(now)
    )
    app.snapshot = make_snapshot(now, make_contract(now, 1, 101, 101, collateral=0))
    write_snapshot(app.snapshot_path, app.snapshot)
    payload = {**planning_payload(), "service_seconds": "0"}
    ranked = app.rank(payload)
    assert ranked["items"][0]["reward_per_hour_isk"] is None
    assert ranked["items"][0]["reward_per_jump_isk"] is None
    assert ranked["items"][0]["reward_to_collateral"] is None
    json.dumps(ranked, allow_nan=False)
    app.solve(payload)
    saved_plan = app.plan_path.read_text()
    app.scan({"regions": [10]})
    assert not app.plan_path.exists()
    assert PlanningSession(tiny_graph, EsiClient(), tmp_path).status()["plan"] is None
    app.plan_path.write_text(saved_plan)
    assert PlanningSession(tiny_graph, EsiClient(), tmp_path).status()["plan"] is None


def test_infeasible_replan_keeps_commitments_and_can_extend_or_record(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    clock = MutableClock(now)
    app = PlanningSession(tiny_graph, EsiClient(), tmp_path, clock=clock)
    app.snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    app.solve(planning_payload())
    app.start_execution({"confirm_locked_acceptance": True})
    assert app.execution is not None
    original = app.execution.active_shipments
    clock.value = app.execution.session_deadline - timedelta(seconds=1)
    assert app.replan({"refresh": False})["plan"]["route"] == []
    assert app.status()["execution"]["active_count"] == 1
    assert not app.status()["plan_armable"]
    clock.value += timedelta(minutes=1)
    app.record_action({"action": "pickup", "contract_id": 1})
    assert app.status()["execution"]["horizon_expired"]
    with pytest.raises(ValueError):
        app.extend_horizon({})
    app.extend_horizon({"minutes": 30})
    assert app.status()["plan"] is None
    assert app.execution.active_shipments[0].deadline == original[0].deadline
    assert app.replan({"refresh": False})["plan"]["certificate"]["feasibility_verified"]
    app.record_action({"action": "delivery", "contract_id": 1})
    assert app.execution.completed_contract_ids == (1,)


def test_departure_revalidation_keeps_absolute_shipment_deadlines(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    planner = CourierPlanner(tiny_graph, EsiClient())
    snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    plan = planner.solve(snapshot, constraints(now, CollateralMode.LOCKED))
    state = planner.arm(plan, at=now)
    state = replace(
        state,
        active_shipments=(
            replace(state.active_shipments[0], deadline=now + timedelta(seconds=30)),
        ),
    )
    plan = planner.replan(snapshot, state)
    assert plan.result.certificate.feasibility_verified
    with pytest.raises(ValueError, match="departure revalidation failed"):
        planner.arm(plan, at=now + timedelta(seconds=31), previous=state)


def test_cli_live_departure_and_embedded_accepted_pickup(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eve_courier_optimizer.cli as cli_module

    clock = MutableClock(now)
    monkeypatch.setattr(cli_module, "load_bundled_graph", lambda: tiny_graph)
    parse_time = cli_module._parse_time
    monkeypatch.setattr(
        cli_module, "_parse_time", lambda value: clock() if value == "now" else parse_time(value)
    )
    snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    snapshot_path, state_path, plan_path = (
        tmp_path / name for name in ("snapshot.json", "state.json", "plan.json")
    )
    write_snapshot(snapshot_path, snapshot)
    args = [
        "solve",
        "--snapshot",
        str(snapshot_path),
        "--start",
        "Alpha",
        "--cargo-m3",
        "1",
        "--collateral-isk",
        "2",
        "--hours",
        "1",
        "--output",
        str(plan_path),
        "--state-output",
        str(state_path),
        "--planning-time",
        "now",
    ]
    assert main(args) == 0
    assert read_execution_state(state_path).current_time == now
    assert (
        main(
            [
                "advance",
                "--state",
                str(state_path),
                "--action",
                "pickup",
                "--contract-id",
                "1",
                "--at",
                now.isoformat(),
                "--output",
                str(state_path),
            ]
        )
        == 0
    )
    assert read_execution_state(state_path).active_shipments[0].picked
    clock.value += timedelta(days=2)
    assert main(args) == 0
    assert json.loads(plan_path.read_text())["summary"]["selected_contract_ids"] == []
