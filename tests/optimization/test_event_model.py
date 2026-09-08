from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime
from itertools import combinations, permutations

import pytest
from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import (
    ActionKind,
    CollateralMode,
    PlannedAction,
    TravelTimeModel,
)
from eve_courier_optimizer.optimization.models.pickup_delivery import PickupDeliveryModel
from eve_courier_optimizer.optimization.models.selection_bounds import build_selection_cuts
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.route_replay import simulate_and_verify
from tests.conftest import make_contract, make_snapshot
from tests.optimization.test_optimizer import constraints


def test_incompatible_contract_arcs_are_omitted_without_removing_either_choice(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now,
            make_contract(now, 1, 101, 103, volume=15, reward=600),
            make_contract(now, 2, 101, 103, volume=15, reward=500),
        ),
        tiny_graph,
        constraints(now, cargo=20, horizon=60),
    )
    model = PickupDeliveryModel(
        problem, selection_hint=(), selection_cuts=build_selection_cuts(problem)
    )
    assert not any(
        {model.events[u].contract_id, model.events[v].contract_id} == {1, 2}
        for u, v in model.arc_is_used
    )
    # Each individually feasible choice remains possible, including the lower-paying job.
    model.model.clear_objective()  # type: ignore[no-untyped-call]
    for contract_id in (1, 2):
        model.model.clear_assumptions()
        model.model.add_assumption(model.contract_is_selected[contract_id])
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        assert solver.solve(model.model) == cp_model.OPTIMAL
        visits, selected = model.extract(solver)
        assert selected == (contract_id,)
        assert simulate_and_verify(problem, tiny_graph, visits, selected).report.valid


def test_terminal_windows_include_delivery_and_preserve_exact_fit(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 102, 103)),
        tiny_graph,
        constraints(now, horizon=42),
    )
    model = PickupDeliveryModel(problem, selection_hint=())
    assert model.latest_arrivals[model.catalog.optional_pickups[1]] == 10
    assert model.latest_arrivals[model.catalog.optional_deliveries[1]] == 21
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(model.model) == cp_model.OPTIMAL
    visits, selected = model.extract(solver)
    replay = simulate_and_verify(problem, tiny_graph, visits, selected)
    assert selected == (1,)
    assert replay.report.valid
    assert replay.finish_seconds == 42


@pytest.mark.parametrize("mode", list(CollateralMode))
@pytest.mark.parametrize("terminal", [None, 1, 3])
def test_reduced_model_matches_independent_route_enumeration(
    now: datetime, tiny_graph: UniverseGraph, mode: CollateralMode, terminal: int | None
) -> None:
    rng = random.Random(97)
    for service in (0, 1):
        contracts = tuple(
            make_contract(
                now,
                i,
                rng.choice((101, 102, 103)),
                rng.choice((101, 102, 103)),
                volume=rng.randint(5, 15),
                collateral=rng.randint(60, 100),
                reward=rng.randint(1, 100),
            )
            for i in (1, 2, 3)
        )
        problem = RouteProblem.from_snapshot(
            make_snapshot(now, *contracts),
            tiny_graph,
            replace(
                constraints(now, cargo=20, collateral=160, horizon=55, mode=mode),
                return_to_start=terminal == 1,
                finish_system_id=terminal if terminal != 1 else None,
                travel=TravelTimeModel(10, service),
            ),
        )
        best = 0
        ids = tuple(c.contract_id for c in problem.contracts)
        for count in range(len(ids) + 1):
            for selected in combinations(ids, count):
                actions = tuple(PlannedAction(kind, cid) for cid in selected for kind in ActionKind)
                for visits in permutations(actions):
                    replay = simulate_and_verify(problem, tiny_graph, visits, selected)
                    if replay.report.valid:
                        best = max(best, replay.total_reward_units)
        model = PickupDeliveryModel(
            problem, selection_hint=(), selection_cuts=build_selection_cuts(problem)
        )
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        assert solver.solve(model.model) == cp_model.OPTIMAL
        assert solver.value(model.total_reward_units) == best
        solved_visits, selected = model.extract(solver)
        assert simulate_and_verify(problem, tiny_graph, solved_visits, selected).report.valid
