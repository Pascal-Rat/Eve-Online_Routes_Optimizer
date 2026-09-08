from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import permutations

import pytest

from eve_courier_optimizer.application.plan_file import solve_result_to_dict
from eve_courier_optimizer.domain import (
    ActionKind,
    CollateralMode,
    PlannedAction,
    PlanningConstraints,
    ProofStatus,
    SecurityPolicy,
    TravelTimeModel,
)
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.optimization.models import selection_bounds as bounds
from eve_courier_optimizer.optimization.models.selection_bounds import build_selection_cuts
from eve_courier_optimizer.optimization.models.system_tour import SystemTourModel
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.exhaustive_optimum import solve_exhaustively
from eve_courier_optimizer.verification.route_replay import simulate_and_verify
from tests.conftest import make_contract, make_snapshot


def constraints(
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


def test_system_relaxation_preserves_shared_route_value(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 103, volume=10, collateral=100, reward=600)
    second = make_contract(now, 2, 101, 103, volume=10, collateral=100, reward=500)
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, first, second),
        tiny_graph,
        constraints(now, horizon=44),
    )

    bound = SystemTourModel(problem).solve(max_time_seconds=2)

    assert bound.status_name == "OPTIMAL"
    assert bound.objective_units == 1_100
    assert bound.upper_bound_units == 1_100


def test_system_relaxation_is_never_below_reference_optimum_on_random_cases(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    rng = random.Random(23)
    stations = (101, 102, 103)
    for case in range(40):
        contracts = []
        for index in range(5):
            origin_index = rng.randrange(0, 2)
            destination_index = rng.randrange(origin_index + 1, 3)
            contracts.append(
                make_contract(
                    now,
                    case * 10 + index + 1,
                    stations[origin_index],
                    stations[destination_index],
                    volume=rng.randint(1, 20),
                    collateral=rng.randint(20, 100),
                    reward=rng.randint(100, 1_000),
                )
            )
        problem = RouteProblem.from_snapshot(
            make_snapshot(now, *contracts),
            tiny_graph,
            replace(
                constraints(now),
                cargo_capacity_units=rng.randint(12, 30),
                collateral_budget_units=rng.randint(100, 220),
                max_simultaneous_contracts=rng.choice((None, 1, 2, 3)),
            ),
        )
        exact = solve_exhaustively(problem, contract_limit=10)
        relaxed = SystemTourModel(problem).solve(max_time_seconds=2)

        assert relaxed.upper_bound_units is not None
        assert exact.objective_units is not None
        assert exact.objective_units <= relaxed.upper_bound_units


def test_rolling_relaxation_does_not_apply_locked_total_collateral(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 102, collateral=100, reward=500)
    second = make_contract(now, 2, 102, 103, collateral=100, reward=700)
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, first, second),
        tiny_graph,
        constraints(now, collateral=100, mode=CollateralMode.ROLLING),
    )

    relaxed = SystemTourModel(problem).solve(max_time_seconds=2)

    assert relaxed.status_name == "OPTIMAL"
    assert relaxed.upper_bound_units == 1_200


def test_pair_conflicts_and_clique_cut_capture_joint_horizon_infeasibility(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contracts = tuple(
        make_contract(now, contract_id, 101, 103, volume=1, collateral=10, reward=100)
        for contract_id in (1, 2, 3)
    )
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *contracts),
        tiny_graph,
        constraints(now, horizon=43),
    )

    cuts = build_selection_cuts(problem)

    assert cuts.pairs == ((1, 2), (1, 3), (2, 3))
    assert cuts.cliques == ((1, 2, 3),)


def test_pair_cut_accounts_for_capacity_when_shared_travel_needs_both_parcels(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 103, volume=15, collateral=10, reward=100)
    second = make_contract(now, 2, 101, 103, volume=15, collateral=10, reward=100)
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, first, second),
        tiny_graph,
        replace(constraints(now, horizon=60), cargo_capacity_units=20),
    )

    cuts = build_selection_cuts(problem)

    assert cuts.pairs == ((1, 2),)


def test_dense_exact_solve_records_and_uses_bound_strengthening(
    now: datetime,
    tiny_graph: UniverseGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the system-master certificate; compact batch certificates have separate coverage.
    monkeypatch.setattr(
        "eve_courier_optimizer.optimization.search.haul_batches.solve_batches",
        lambda *args, **kwargs: None,
    )
    contracts = tuple(
        make_contract(now, contract_id, 101, 103, volume=1, collateral=1, reward=100)
        for contract_id in range(1, 21)
    )
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *contracts),
        tiny_graph,
        constraints(now, horizon=100),
    )

    result = RouteOptimizer(
        problem,
        tiny_graph,
        config=SolverConfig(
            max_time_seconds=5,
            relaxation_time_seconds=2,
            independent_reference_limit=0,
        ),
    ).solve()

    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.total_reward_units == 2_000
    assert result.certificate.system_relaxation_status == "OPTIMAL"
    assert result.certificate.system_relaxation_bound_units == 2_000
    assert result.certificate.system_relaxation_systems == 2
    assert result.certificate.decomposition_status == "bound_matched"
    assert result.certificate.decomposition_iterations == 1
    assert result.certificate.decomposition_learned_cuts == 0
    assert result.certificate.decomposition_proof_closed
    payload = solve_result_to_dict(result, problem)
    assert payload["certificate"]["bound_strengthening"]["system_relaxation_bound_units"] == 2_000
    assert payload["certificate"]["bound_strengthening"]["decomposition_proof_closed"] is True


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
    problem = RouteProblem.from_snapshot(
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
    # Isolate the packing transform; integer crossing bounds independently close this gap.
    monkeypatch.setattr(
        "eve_courier_optimizer.optimization.models.system_tour.add_resource_crossing_bounds",
        lambda *args: (),
    )
    specs = bounds._resource_work_specs
    with monkeypatch.context() as patch:
        patch.setattr(
            bounds,
            "_resource_work_specs",
            lambda p: [s for s in specs(p) if not isinstance(s, bounds._LiftedResourceWorkSpec)],
        )
        old = SystemTourModel(problem, selection_cuts=bounds.SelectionCuts((), ())).solve(
            max_time_seconds=2
        )
    new = SystemTourModel(problem, selection_cuts=bounds.SelectionCuts((), ())).solve(
        max_time_seconds=2
    )
    assert old.upper_bound_units == 300
    assert new.upper_bound_units == solve_exhaustively(problem).objective_units == 200


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
    problem = RouteProblem.from_snapshot(
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
    assert len(problem.contracts) == 2
    cuts = bounds.build_selection_cuts(problem)
    assert bool(cuts.pairs) is incompatible
    # Independently enumerate every event ordering, with the verifier handling time semantics.
    actions = tuple(PlannedAction(action, i) for i in (1, 2) for action in ActionKind)
    feasible = any(
        simulate_and_verify(problem, tiny_graph, order, (1, 2)).report.valid
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
        problem = RouteProblem.from_snapshot(
            make_snapshot(
                now, *(replace(job, date_expired=now + timedelta(seconds=expiry)) for job in jobs)
            ),
            tiny_graph,
            c,
        )
        assert bool(bounds.build_selection_cuts(problem).pairs) is cut
    # Together both jobs finish exactly at their locked deadline; rejecting equality is unsound.
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *(replace(job, volume_units=40) for job in jobs)),
        tiny_graph,
        replace(
            c,
            horizon_seconds=172800,
            collateral_mode=CollateralMode.LOCKED,
            travel=TravelTimeModel(43000, 100),
        ),
    )
    assert bounds.build_selection_cuts(problem).pairs == ()


def test_master_must_allow_pickup_delivery_system_revisits(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 101, 103), make_contract(now, 2, 103, 101)),
        tiny_graph,
        constraints(now, horizon=50),
    )
    assert bounds.build_selection_cuts(problem).pairs == ()
    assert SystemTourModel(problem).solve(max_time_seconds=2).upper_bound_units == 1000
    assert solve_exhaustively(problem).objective_units == 1000
