from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eve_courier_optimizer.application.planner import CourierPlanner
from eve_courier_optimizer.application.trip_file import read_trip, write_trip
from eve_courier_optimizer.cli import main
from eve_courier_optimizer.domain import CollateralMode
from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.eve.snapshot_file import write_snapshot
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.workspace import PlanningWorkspace
from tests.support.assertions import present
from tests.support.clock import MutableClock
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.scenarios import trip_constraints as constraints
from tests.support.web import CourierTransport, planning_payload, proposal_input, seed_snapshot


def test_failed_reset_keeps_live_commitments(
    now: datetime, tiny_graph: UniverseGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = PlanningWorkspace(tiny_graph, EsiClient(), tmp_path, clock=MutableClock(now))
    seed_snapshot(app, make_snapshot(now, make_contract(now, 1, 101, 102)))
    app.solve(planning_payload())
    app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
    state = app.trip
    assert state is not None and state.active_shipments
    saved = app.store.path.read_bytes()
    original_replace = Path.replace

    def fail(path: Path, target: str | Path) -> Path:
        if target == app.store.path:
            raise OSError("disk unavailable")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        app.reset_execution()
    assert app.trip == state
    assert app.store.path.read_bytes() == saved
    assert PlanningWorkspace(tiny_graph, EsiClient(), tmp_path).trip == state


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
    picked = state.pick_up(None, tiny_graph, 1, now)
    with pytest.raises(ValueError, match="cargo capacity"):
        picked.pick_up(None, tiny_graph, 2, now)
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
    picked = state.pick_up(None, tiny_graph, 1, later)
    with pytest.raises(ValueError, match="extend the horizon"):
        picked.replanning_constraints(snapshot)
    extended = picked.extend_horizon(additional_seconds=3600, at=later)
    assert extended.active_shipments == picked.active_shipments
    assert extended.remaining_required_system_ids == picked.remaining_required_system_ids
    assert extended.terminal_system_id == picked.terminal_system_id
    assert extended.replanning_constraints(snapshot).horizon_seconds == 3600
    delivered = picked.deliver(1, later + timedelta(minutes=1))
    reached = delivered.reach_system(3, later + timedelta(minutes=2))
    path = tmp_path / "state.json"
    write_trip(path, reached)
    assert read_trip(path) == reached
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
    assert read_trip(path).session_deadline == reached.current_time + timedelta(minutes=30)
    with pytest.raises(ValueError, match="deadline"):
        picked.deliver(1, now + timedelta(days=2))
    with pytest.raises(ValueError, match="deadline"):
        state.pick_up(None, tiny_graph, 1, now + timedelta(days=2))
    with pytest.raises(ValueError):
        picked.extend_horizon(additional_seconds=0, at=later)
    with pytest.raises(ValueError):
        picked.extend_horizon(additional_seconds=60, at=now)


def test_live_clock_filters_old_listings_and_revalidates_departure(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    clock = MutableClock(now)
    app = PlanningWorkspace(tiny_graph, EsiClient(), tmp_path, clock=clock)
    contract = replace(make_contract(now, 1, 101, 102), date_expired=now + timedelta(minutes=1))
    seed_snapshot(app, make_snapshot(now, contract))
    app.solve(planning_payload())
    assert app.plan is not None
    assert app.plan.problem.constraints.snapshot_time == now
    clock.value += timedelta(minutes=2)
    with pytest.raises(ValueError, match="listing has expired"):
        app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
    assert app.trip is None
    assert app.solve(planning_payload())["plan"]["summary"]["selected_contract_ids"] == []
    assert app.plan.problem.constraints.snapshot_time == clock.value


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
    app = PlanningWorkspace(
        tiny_graph, EsiClient(transport=CourierTransport(now)), tmp_path, clock=MutableClock(now)
    )
    seed_snapshot(app, make_snapshot(now, make_contract(now, 1, 101, 101, collateral=0)))
    payload = {**planning_payload(), "service_seconds": "0"}
    ranked = app.rank(payload)
    assert ranked["items"][0]["reward_per_hour_isk"] is None
    assert ranked["items"][0]["reward_per_jump_isk"] is None
    assert ranked["items"][0]["reward_to_collateral"] is None
    json.dumps(ranked, allow_nan=False)
    app.solve(payload)
    saved_plan = json.dumps(app.artifact("plan.json"))
    app.scan({"regions": [10]})
    assert app.artifact("plan.json") is None
    assert PlanningWorkspace(tiny_graph, EsiClient(), tmp_path).status()["plan"] is None
    (app.workspace / "plan.json").write_text(saved_plan)
    assert PlanningWorkspace(tiny_graph, EsiClient(), tmp_path).status()["plan"] is None


def test_infeasible_replan_keeps_commitments_and_can_extend_or_record(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    clock = MutableClock(now)
    app = PlanningWorkspace(tiny_graph, EsiClient(), tmp_path, clock=clock)
    seed_snapshot(app, make_snapshot(now, make_contract(now, 1, 101, 102)))
    app.solve(planning_payload())
    app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
    assert app.trip is not None
    original = app.trip.active_shipments
    clock.value = app.trip.session_deadline - timedelta(seconds=1)
    assert app.replan({"refresh": False})["plan"]["route"] == []
    assert present(app.status()["execution"])["active_count"] == 1
    assert not app.status()["plan_armable"]
    clock.value += timedelta(minutes=1)
    app.record_action({"action": "pickup", "contract_id": 1})
    assert present(app.status()["execution"])["horizon_expired"]
    with pytest.raises(ValueError):
        app.extend_horizon({})
    app.extend_horizon({"minutes": 30})
    assert app.status()["plan"] is None
    assert app.trip.active_shipments[0].deadline == original[0].deadline
    assert app.replan({"refresh": False})["plan"]["certificate"]["feasibility_verified"]
    app.record_action({"action": "delivery", "contract_id": 1})
    assert app.trip.completed_contract_ids == (1,)


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
    # Exercise the CLI's clock seam without changing its public command interface.
    parse_time = cli_module._parse_time  # pyright: ignore[reportPrivateUsage]

    def parse_at_clock(value: str) -> datetime:
        return clock() if value == "now" else parse_time(value)

    monkeypatch.setattr(cli_module, "_parse_time", parse_at_clock)
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
    assert read_trip(state_path).current_time == now
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
    assert read_trip(state_path).active_shipments[0].picked
    clock.value += timedelta(days=2)
    assert main(args) == 0
    assert json.loads(plan_path.read_text())["summary"]["selected_contract_ids"] == []


@pytest.mark.parametrize("invalid_input", [{"time_limit": "nan"}, {"max_candidates": 0}])
def test_invalid_replan_cannot_refresh_the_saved_snapshot(
    invalid_input: dict[str, object], now: datetime, tiny_graph: UniverseGraph, tmp_path: Path
) -> None:
    transport = CourierTransport(now)
    workspace = PlanningWorkspace(
        tiny_graph, EsiClient(transport=transport), tmp_path, clock=lambda: now
    )
    workspace.scan({"regions": [10]})
    workspace.solve(planning_payload())
    workspace.start_execution(proposal_input(workspace, {"confirm_locked_acceptance": True}))
    snapshot = workspace.snapshot
    saved_snapshot = workspace.store.path.read_bytes()
    calls = transport.calls

    with pytest.raises(ValueError):
        workspace.replan({"refresh": True, **invalid_input})

    assert transport.calls == calls
    assert workspace.snapshot is snapshot
    assert workspace.store.path.read_bytes() == saved_snapshot
