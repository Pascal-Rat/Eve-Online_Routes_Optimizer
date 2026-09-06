from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from itertools import permutations

import pytest

from eve_courier_optimizer import bounds
from eve_courier_optimizer.construction import improve_incumbent, insert_additional_contracts
from eve_courier_optimizer.domain import ActionKind, CollateralMode, TravelTimeModel
from eve_courier_optimizer.planning import prepare_problem
from eve_courier_optimizer.reference_solver import solve_reference
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.solver import _build_greedy_route_hint
from eve_courier_optimizer.verification import (
    PlannedAction,
    PlannedWaypoint,
    simulate_and_verify,
)

from .conftest import make_contract, make_snapshot
from .test_bounds import constraints


def test_lifted_transform_preserves_every_small_integer_packing() -> None:
    # Dynamic programming enumerates all multisets fitting each capacity, including exact fits.
    for capacity in range(2, 21):
        for threshold in range(1, capacity // 2 + 1):
            spec = bounds._LiftedResourceWorkSpec(capacity, "volume", threshold)
            best = [0] * (capacity + 1)
            for load in range(1, capacity + 1):
                best[load] = max(best[load - w] + spec.demand(w) for w in range(1, load + 1))
                assert best[load] <= capacity
            assert spec.demand(threshold) == threshold
            assert spec.demand(capacity - threshold) == capacity - threshold


def test_lifted_work_tightens_mixed_load_bound(
    now: datetime, tiny_graph: UniverseGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare_problem(
        make_snapshot(
            now,
            *(
                make_contract(now, i, 101, 103, volume=w, reward=100)
                for i, w in enumerate((75, 40, 40), 1)
            ),
        ),
        tiny_graph,
        replace(constraints(now, horizon=70), travel=TravelTimeModel(10, 0)),
    )
    specs = bounds._resource_work_specs
    with monkeypatch.context() as patch:
        patch.setattr(
            bounds,
            "_resource_work_specs",
            lambda p: [s for s in specs(p) if not isinstance(s, bounds._LiftedResourceWorkSpec)],
        )
        old = bounds.solve_system_relaxation(
            prepared, max_time_seconds=2, selection_cuts=bounds.SelectionCuts((), ())
        )
    new = bounds.solve_system_relaxation(
        prepared, max_time_seconds=2, selection_cuts=bounds.SelectionCuts((), ())
    )
    assert old.upper_bound_units == 300
    assert new.upper_bound_units == solve_reference(prepared).objective_units == 200


def test_reconstruction_replaces_blocking_choice_and_preserves_waypoints(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    prepared = prepare_problem(
        make_snapshot(
            now,
            make_contract(now, 1, 101, 101, collateral=100, reward=600),
            *(
                make_contract(now, i, 101, 103, volume=40, collateral=50, reward=500)
                for i in (2, 3)
            ),
        ),
        tiny_graph,
        replace(constraints(now, horizon=80, collateral=100), required_system_ids=frozenset({2})),
    )
    visits = _build_greedy_route_hint(prepared)
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    seed = simulate_and_verify(prepared.problem, tiny_graph, visits, ids)
    _, old = insert_additional_contracts(prepared, tiny_graph, visits, seed)
    assert old.total_reward_units == 600
    result, improved = improve_incumbent(
        prepared,
        tiny_graph,
        visits,
        seed,
        restart_visits=(PlannedWaypoint(2),),
    )
    assert improved.report.valid
    assert improved.total_reward_units == 1000
    assert {v.contract_id for v in result if isinstance(v, PlannedAction)} == {2, 3}
    # A bad restart must never erase an already verified incumbent.
    _, retained = improve_incumbent(prepared, tiny_graph, visits, seed, restart_visits=())
    assert retained == old


@pytest.mark.parametrize(
    "mode, expiry_hours, incompatible",
    [
        (CollateralMode.LOCKED, 72, True),
        (CollateralMode.ROLLING, 1, True),
        (CollateralMode.ROLLING, 72, False),
    ],
)
def test_pair_projection_respects_absolute_and_rolling_deadlines(
    now: datetime,
    tiny_graph: UniverseGraph,
    mode: CollateralMode,
    expiry_hours: int,
    incompatible: bool,
) -> None:
    prepared = prepare_problem(
        make_snapshot(
            now,
            *(
                make_contract(now, i, 101, 103, volume=60, expiry_hours=expiry_hours)
                for i in (1, 2)
            ),
        ),
        tiny_graph,
        replace(constraints(now, horizon=180_000, mode=mode), travel=TravelTimeModel(20_000, 100)),
    )
    assert len(prepared.problem.contracts) == 2
    cuts = bounds.build_selection_cuts(prepared)
    assert bool(cuts.pairs) is incompatible
    # Independently enumerate every event ordering, with the verifier handling time semantics.
    actions = tuple(PlannedAction(action, i) for i in (1, 2) for action in ActionKind)
    feasible = any(
        simulate_and_verify(prepared.problem, tiny_graph, order, (1, 2)).report.valid
        for order in permutations(actions)
    )
    assert feasible is not incompatible


def test_pair_projection_expiry_is_strict_but_completion_deadline_is_inclusive(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    # One-parcel capacity forces the second pickup to occur after the first complete loop.
    jobs = tuple(make_contract(now, i, 101, 103, volume=60) for i in (1, 2))
    c = replace(
        constraints(now, horizon=100, mode=CollateralMode.ROLLING), travel=TravelTimeModel(10, 1)
    )
    for expiry, cut in ((42, True), (43, False)):
        prepared = prepare_problem(
            make_snapshot(
                now, *(replace(job, date_expired=now + timedelta(seconds=expiry)) for job in jobs)
            ),
            tiny_graph,
            c,
        )
        assert bool(bounds.build_selection_cuts(prepared).pairs) is cut
    # Together both jobs finish exactly at their locked deadline; rejecting equality is unsound.
    prepared = prepare_problem(
        make_snapshot(now, *(replace(job, volume_units=40) for job in jobs)),
        tiny_graph,
        replace(
            c,
            horizon_seconds=172800,
            collateral_mode=CollateralMode.LOCKED,
            travel=TravelTimeModel(43000, 100),
        ),
    )
    assert bounds.build_selection_cuts(prepared).pairs == ()


def test_master_must_allow_pickup_delivery_system_revisits(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    prepared = prepare_problem(
        make_snapshot(now, make_contract(now, 1, 101, 103), make_contract(now, 2, 103, 101)),
        tiny_graph,
        constraints(now, horizon=50),
    )
    assert bounds.build_selection_cuts(prepared).pairs == ()
    assert bounds.solve_system_relaxation(prepared, max_time_seconds=2).upper_bound_units == 1000
    assert solve_reference(prepared).objective_units == 1000
