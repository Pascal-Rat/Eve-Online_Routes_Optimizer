from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import ActiveShipment, CollateralMode, TravelTimeModel
from eve_courier_optimizer.optimization.models.pickup_delivery import PickupDeliveryModel
from eve_courier_optimizer.optimization.models.selection_bounds import (
    SubsetRewardCut,
    add_subset_reward_cut,
)
from eve_courier_optimizer.optimization.search.route_insertion import build_greedy_route_hint
from eve_courier_optimizer.optimization.search.subset_search import solve_subset
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.exhaustive_optimum import solve_exhaustively
from eve_courier_optimizer.verification.route_replay import simulate_and_verify
from tests.conftest import make_contract, make_snapshot
from tests.optimization.test_reward_bounds import constraints


def test_subset_search_matches_independent_reference_and_verifies_witnesses(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    rng = random.Random(807)
    for case in range(60):
        items = tuple(
            make_contract(
                now,
                i + 1,
                rng.choice((101, 102, 103)),
                rng.choice((101, 102, 103)),
                volume=rng.randint(0, 70),
                collateral=rng.randint(0, 80),
                reward=rng.randint(0, 1000),
            )
            for i in range(6)
        )
        c = replace(
            constraints(now, horizon=rng.randint(30, 200)),
            cargo_capacity_units=rng.randint(20, 100),
            collateral_budget_units=rng.randint(100, 300),
            travel=TravelTimeModel(10, rng.choice((0, 1, 5))),
            return_to_start=case % 3 == 0,
            finish_system_id=3 if case % 3 == 1 else None,
            max_simultaneous_contracts=rng.choice((None, 1, 2)),
        )
        p = RouteProblem.from_snapshot(make_snapshot(now, *items), tiny_graph, c)
        answer = solve_subset(p, max_time_seconds=3)
        assert answer is not None and answer.complete
        assert (
            answer.objective_units
            == answer.upper_bound_units
            == solve_exhaustively(p).objective_units
        )
        simulation = simulate_and_verify(p, tiny_graph, answer.visits, answer.selected_contract_ids)
        assert simulation.report.valid and simulation.total_reward_units == answer.objective_units
        if answer.infeasible_core_ids:
            core = set(answer.infeasible_core_ids)
            for removed in (None, *core):
                subset = replace(
                    p,
                    contracts=tuple(
                        i for i in p.contracts if i.contract_id in core and i.contract_id != removed
                    ),
                )
                feasible = solve_exhaustively(subset).objective_units == sum(
                    i.reward_units for i in subset.contracts
                )
                assert feasible == (removed is not None)


def test_subset_search_rolling_expiry_matches_event_model(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    rng = random.Random(971)
    for case in range(25):
        items = tuple(
            replace(
                make_contract(
                    now,
                    i + 1,
                    rng.choice((101, 102, 103)),
                    rng.choice((101, 102, 103)),
                    volume=rng.randint(0, 40),
                    collateral=rng.randint(10, 80),
                    reward=rng.randint(1, 1000),
                ),
                date_expired=now + timedelta(seconds=rng.randint(1, 80), microseconds=case % 2),
            )
            for i in range(5)
        )
        c = replace(
            constraints(now, horizon=100, collateral=100, mode=CollateralMode.ROLLING),
            cargo_capacity_units=60,
            travel=TravelTimeModel(10, case % 3),
            return_to_start=case % 2 == 0,
            max_simultaneous_contracts=2,
        )
        p = RouteProblem.from_snapshot(make_snapshot(now, *items), tiny_graph, c)
        answer = solve_subset(p, max_time_seconds=3)
        assert answer is not None and answer.complete
        route = PickupDeliveryModel(p, selection_hint=build_greedy_route_hint(p))
        cp = cp_model.CpSolver()
        cp.parameters.num_search_workers = 1
        cp.parameters.max_time_in_seconds = 3
        assert cp.solve(route.model) == cp_model.OPTIMAL
        assert answer.upper_bound_units == cp.value(route.total_reward_units)
        simulation = simulate_and_verify(p, tiny_graph, answer.visits, answer.selected_contract_ids)
        assert simulation.report.valid and simulation.total_reward_units == answer.objective_units


def test_subset_limits_never_convert_an_incumbent_into_a_ceiling(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    p = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    for max_states, max_time_seconds in ((1, 2.0), (250_000, 1e-12)):
        answer = solve_subset(p, max_states=max_states, max_time_seconds=max_time_seconds)
        assert answer is not None and not answer.complete
        assert answer.objective_units == 0 and answer.upper_bound_units is None
        assert simulate_and_verify(
            p, tiny_graph, answer.visits, answer.selected_contract_ids
        ).report.valid
    assert solve_subset(p, max_time_seconds=1, contract_limit=0) is None


def test_subset_guards_and_infeasible_terminal(now: datetime, tiny_graph: UniverseGraph) -> None:
    p = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    for c in (
        replace(p.constraints, required_system_ids=frozenset({2})),
        replace(p.constraints, collateral_mode=CollateralMode.ROLLING, horizon_seconds=86_401),
    ):
        assert solve_subset(replace(p, constraints=c), max_time_seconds=1) is None
    active = ActiveShipment(p.contracts[0], now + timedelta(hours=1), picked=True)
    assert solve_subset(replace(p, active_shipments=(active,)), max_time_seconds=1) is None
    c = replace(p.constraints, return_to_start=False, finish_system_id=3, horizon_seconds=1)
    answer = solve_subset(replace(p, constraints=c), max_time_seconds=1)
    assert answer is not None and answer.complete and answer.objective_units is None
    for limit in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            solve_subset(p, max_time_seconds=limit)


def test_subset_reward_cut_excludes_more_than_the_full_selection(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    # Every positive-reward pair is impossible, even though the full-set no-good permits pairs.
    p = RouteProblem.from_snapshot(
        make_snapshot(
            now, *(make_contract(now, i, 101, 103, volume=60, reward=100 * i) for i in range(1, 4))
        ),
        tiny_graph,
        constraints(now, horizon=50),
    )
    answer = solve_subset(p, max_time_seconds=1)
    assert answer is not None and answer.upper_bound_units == 300
    route = PickupDeliveryModel(p, selection_hint=build_greedy_route_hint(p))
    cut = SubsetRewardCut(tuple((i.contract_id, i.reward_units) for i in p.contracts), 300)
    add_subset_reward_cut(route.model, route.contract_is_selected, cut)
    cp = cp_model.CpSolver()
    assert cp.solve(route.model) == cp_model.OPTIMAL
    visits, ids = route.extract(cp)
    assert simulate_and_verify(p, tiny_graph, visits, ids).total_reward_units == 300
    route.model.add(route.contract_is_selected[2] == 1)
    route.model.add(route.contract_is_selected[3] == 1)
    assert cp.solve(route.model) == cp_model.INFEASIBLE


def test_subset_respects_binding_locked_completion_deadlines(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    c = replace(
        constraints(now, horizon=100_000),
        return_to_start=False,
        cargo_capacity_units=60,
        travel=TravelTimeModel(30_000, 0),
    )
    p = RouteProblem.from_snapshot(
        make_snapshot(
            now,
            make_contract(now, 1, 101, 102, volume=60, reward=100),
            make_contract(now, 2, 101, 102, volume=60, reward=200),
        ),
        tiny_graph,
        c,
    )
    answer = solve_subset(p, max_time_seconds=1)
    assert answer is not None and answer.complete
    # Both trips fit the horizon, but the second delivery would miss its one-day deadline.
    assert answer.upper_bound_units == 200 == solve_exhaustively(p).objective_units
    assert answer.infeasible_core_ids == (1, 2)
