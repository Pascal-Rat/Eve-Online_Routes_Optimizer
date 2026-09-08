from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import combinations, permutations
from unittest.mock import Mock

import pytest
from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import (
    ActionKind,
    ActiveShipment,
    CollateralMode,
    PlannedAction,
    PlannedVisit,
    PlannedWaypoint,
    RoutableContract,
    TravelTimeModel,
)
from eve_courier_optimizer.optimization.models.selection_bounds import (
    SelectionCuts,
    add_resource_crossing_bounds,
)
from eve_courier_optimizer.optimization.models.system_tour import SystemTourModel
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import Region, SdeMetadata, SolarSystem, UniverseGraph
from eve_courier_optimizer.verification.exhaustive_optimum import solve_exhaustively
from eve_courier_optimizer.verification.route_replay import VerifiedRoute, simulate_and_verify
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.scenarios import reward_constraints as constraints


def test_integer_crossings_rule_out_a_fractional_return_trip(
    now: datetime, tiny_graph: UniverseGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now, *(make_contract(now, i, 101, 102, volume=40, reward=100) for i in (1, 2, 3))
        ),
        tiny_graph,
        replace(constraints(now, horizon=3), travel=TravelTimeModel(1, 0)),
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "eve_courier_optimizer.optimization.models.system_tour.add_resource_crossing_bounds",
            Mock(return_value=()),
        )
        relaxed = SystemTourModel(problem, selection_cuts=SelectionCuts((), ())).solve(
            max_time_seconds=2
        )
    strengthened = SystemTourModel(problem, selection_cuts=SelectionCuts((), ())).solve(
        max_time_seconds=2
    )
    assert relaxed.upper_bound_units == 300
    assert strengthened.upper_bound_units == 200
    # Two 40-unit parcels fit together; the third needs a second two-jump round trip.
    exact_fit = replace(problem, constraints=replace(problem.constraints, horizon_seconds=4))
    assert SystemTourModel(exact_fit).solve(max_time_seconds=2).upper_bound_units == 300


@pytest.mark.parametrize("mode", list(CollateralMode))
@pytest.mark.parametrize("terminal", [None, 1, 3])
@pytest.mark.parametrize("obligation", ["none", "picked", "unpicked", "waypoint"])
def test_crossing_bounds_preserve_every_enumerated_feasible_selection(
    now: datetime,
    tiny_graph: UniverseGraph,
    mode: CollateralMode,
    terminal: int | None,
    obligation: str,
) -> None:
    rng = random.Random(1307 + (terminal or 0))
    for service in (0, 1):
        items = tuple(
            make_contract(
                now,
                i,
                rng.choice((101, 102, 103)),
                rng.choice((101, 102, 103)),
                volume=rng.choice((0, 4, 6, 10)),
                collateral=rng.choice((0, 60, 100)),
                reward=rng.choice((0, 17, 53)),
            )
            for i in (7, 19)
        )
        active: tuple[ActiveShipment, ...] = ()
        if obligation in {"picked", "unpicked"}:
            active = (
                ActiveShipment(
                    RoutableContract.resolve(
                        make_contract(now, 99, 102, 103, volume=4, collateral=40), 2, 3
                    ),
                    now + timedelta(seconds=25),
                    picked=obligation == "picked",
                ),
            )
        problem = RouteProblem.from_snapshot(
            make_snapshot(now, *items),
            tiny_graph,
            replace(
                constraints(now, horizon=35, collateral=160, mode=mode),
                cargo_capacity_units=10,
                max_simultaneous_contracts=2,
                travel=TravelTimeModel(5, service),
                return_to_start=terminal == 1,
                finish_system_id=terminal if terminal != 1 else None,
                required_system_ids=frozenset({2}) if obligation == "waypoint" else frozenset(),
            ),
            active_shipments=active,
        )
        ids = tuple(item.contract_id for item in problem.contracts)
        for count in range(len(ids) + 1):
            for selected in combinations(ids, count):
                actions: tuple[PlannedVisit, ...] = tuple(
                    PlannedAction(action, cid) for cid in selected for action in ActionKind
                )
                for shipment in active:
                    if not shipment.picked:
                        actions += (
                            PlannedAction(ActionKind.PICKUP, shipment.contract.contract_id),
                        )
                    actions += (PlannedAction(ActionKind.DELIVERY, shipment.contract.contract_id),)
                actions += tuple(
                    PlannedWaypoint(s) for s in problem.constraints.required_system_ids
                )
                for visits in permutations(actions):
                    replay = simulate_and_verify(problem, tiny_graph, visits, selected)
                    if not replay.report.valid:
                        continue
                    master = SystemTourModel(problem)
                    master.install_incumbent(
                        VerifiedRoute.verify(problem, tiny_graph, visits, selected)
                    )
                    solver = cp_model.CpSolver()
                    solver.parameters.fix_variables_to_their_hinted_value = True
                    solver.parameters.num_search_workers = 1
                    assert solver.solve(master.model) == cp_model.OPTIMAL
                    break


def test_crossing_bounds_skip_asymmetric_metrics_and_unsafe_coefficients(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    asymmetric = replace(problem, jump_matrix={**problem.jump_matrix, (3, 1): 3})
    enormous = replace(problem, constraints=replace(problem.constraints, horizon_seconds=2**61))
    for candidate in (asymmetric, enormous):
        model = cp_model.CpModel()
        selected = {1: model.new_bool_var("selected")}
        assert add_resource_crossing_bounds(model, candidate, selected) == ()
        assert model.validate() == ""


@pytest.mark.parametrize("seed", [811, 813, 827, 829])
def test_master_bound_dominates_independent_optimum_after_graph_relabeling(
    now: datetime, seed: int
) -> None:
    rng = random.Random(seed)
    size = 6
    edges: dict[int, set[int]] = {s: set() for s in range(size)}
    for target in range(1, size):
        source = rng.randrange(target)
        edges[source].add(target)
        edges[target].add(source)
    for _ in range(seed % 3):
        source, target = rng.sample(range(size), 2)
        edges[source].add(target)
        edges[target].add(source)
    jobs = tuple(
        make_contract(
            now,
            cid,
            *rng.sample(range(100, 100 + size), 2),
            volume=rng.choice((0, 4, 6, 10)),
            collateral=20,
            reward=rng.randint(1, 100),
        )
        for cid in (11, 29, 83, 97)
    )
    results: list[tuple[int, int]] = []
    for labels, ordered_jobs in (
        (tuple(range(1, size + 1)), jobs),
        (tuple(rng.sample(range(1000, 9000), size)), tuple(reversed(jobs))),
    ):
        graph = UniverseGraph(
            systems={s: SolarSystem(s, 10, str(s), 0.9) for s in labels},
            adjacency={labels[s]: tuple(labels[t] for t in edges[s]) for s in edges},
            station_systems={100 + i: s for i, s in enumerate(labels)},
            regions={10: Region(10, "Generated")},
            metadata=SdeMetadata(1, now.isoformat(), "test://crossing-graphs"),
        )
        problem = RouteProblem.from_snapshot(
            make_snapshot(now, *ordered_jobs),
            graph,
            replace(
                constraints(now, horizon=40, collateral=60),
                cargo_capacity_units=10,
                travel=TravelTimeModel(3, seed % 2),
                start_system_id=labels[0],
                return_to_start=seed % 3 == 0,
                finish_system_id=labels[-1] if seed % 3 == 1 else None,
            ),
        )
        reference = solve_exhaustively(problem)
        master = SystemTourModel(problem).solve(max_time_seconds=5)
        assert reference.complete and reference.objective_units is not None
        assert master.status_name == "OPTIMAL" and master.upper_bound_units is not None
        assert master.upper_bound_units >= reference.objective_units
        results.append((reference.objective_units, master.upper_bound_units))
    assert results[0] == results[1]
