from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from ortools.sat.python import cp_model

from eve_courier_optimizer.batch_search import solve_batches
from eve_courier_optimizer.domain import ActiveShipment, CollateralMode, TravelTimeModel
from eve_courier_optimizer.planning import prepare_problem
from eve_courier_optimizer.reference_solver import solve_reference
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.solver import SolverConfig, _run_dense_decomposition, solve_exact
from eve_courier_optimizer.verification import simulate_and_verify

from .conftest import make_contract, make_snapshot
from .test_bounds import constraints


def test_batches_match_exhaustive_optima_across_route_shapes(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    rng = random.Random(8203)
    for _case in range(100):
        origin, destination = rng.choice(((101, 103), (103, 101)))
        items = tuple(
            make_contract(
                now,
                i + 1,
                origin,
                destination,
                volume=rng.randint(0, 60),
                collateral=rng.randint(0, 90),
                reward=rng.randint(0, 1000),
            )
            for i in range(rng.randint(1, 7))
        )
        c = replace(
            constraints(now, horizon=rng.randint(20, 250)),
            start_system_id=rng.choice((1, 2, 3)),
            return_to_start=False,
            finish_system_id=rng.choice((None, 1, 2, 3)),
            cargo_capacity_units=rng.randint(30, 150),
            collateral_budget_units=rng.randint(90, 500),
            max_simultaneous_contracts=rng.choice((None, 1, 2)),
            travel=TravelTimeModel(10, rng.choice((0, 2, 5))),
        )
        p = prepare_problem(make_snapshot(now, *items), tiny_graph, c)
        answer = solve_batches(p, max_time_seconds=2)
        if not p.problem.contracts:
            assert answer is None
            continue
        assert answer is not None and answer.complete
        assert (
            answer.upper_bound_units == answer.objective_units == solve_reference(p).objective_units
        )
        sim = simulate_and_verify(
            p.problem, tiny_graph, answer.visits, answer.selected_contract_ids
        )
        assert sim.report.valid and sim.total_reward_units == answer.objective_units


def test_batch_guards_preserve_general_solver_cases(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    p = prepare_problem(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    for c in (
        replace(p.problem.constraints, collateral_mode=CollateralMode.ROLLING),
        replace(p.problem.constraints, horizon_seconds=86_401),
        replace(p.problem.constraints, required_system_ids=frozenset({2})),
        replace(
            p.problem.constraints, return_to_start=False, finish_system_id=3, horizon_seconds=1
        ),
    ):
        assert (
            solve_batches(replace(p, problem=replace(p.problem, constraints=c)), max_time_seconds=1)
            is None
        )
    active = ActiveShipment(p.problem.contracts[0], now + timedelta(hours=1), picked=True)
    assert (
        solve_batches(
            replace(p, problem=replace(p.problem, active_shipments=(active,))), max_time_seconds=1
        )
        is None
    )
    for items in (
        (make_contract(now, 1, 101, 101),),
        (make_contract(now, 1, 101, 103), make_contract(now, 2, 102, 103)),
    ):
        assert (
            solve_batches(
                prepare_problem(make_snapshot(now, *items), tiny_graph, constraints(now)),
                max_time_seconds=1,
            )
            is None
        )
    answer = solve_batches(p, max_time_seconds=1e-12)
    assert answer is not None and not answer.complete and answer.upper_bound_units is None
    with pytest.raises(ValueError):
        solve_batches(p, max_time_seconds=0)


def test_dense_batch_proof_avoids_full_event_model(
    now: datetime, tiny_graph: UniverseGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = prepare_problem(
        make_snapshot(
            now, *(make_contract(now, i, 101, 103, volume=40, reward=i) for i in range(1, 41))
        ),
        tiny_graph,
        replace(constraints(now, horizon=100), collateral_budget_units=10000),
    )

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("the compact proof should avoid the full event model")

    monkeypatch.setattr("eve_courier_optimizer.solver._build_model", unexpected)
    result = solve_exact(p, tiny_graph, config=SolverConfig(minimize_finish_time_after_proof=False))
    assert result.certificate.status.value == "proven_optimal"
    assert result.certificate.decomposition_status == "batch_bound_matched"
    assert result.certificate.feasibility_verified and result.certificate.scope_untruncated


def test_unknown_batch_response_has_no_default_zero_ceiling(
    now: datetime,
    tiny_graph: UniverseGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = prepare_problem(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    monkeypatch.setattr(cp_model.CpSolver, "solve", lambda *args, **kwargs: cp_model.UNKNOWN)
    answer = solve_batches(p, max_time_seconds=1)
    assert answer is not None and not answer.complete and answer.upper_bound_units is None


def test_batch_witness_and_ceiling_survive_unknown_master(
    now: datetime,
    tiny_graph: UniverseGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eve_courier_optimizer.bounds import SystemRelaxationBound

    p = prepare_problem(
        make_snapshot(
            now, *(make_contract(now, i, 101, 103, volume=40, reward=i) for i in range(1, 21))
        ),
        tiny_graph,
        replace(constraints(now, horizon=100), collateral_budget_units=10000),
    )
    answer = solve_batches(p, max_time_seconds=2)
    assert answer is not None and answer.upper_bound_units is not None
    partial = replace(answer, complete=False, upper_bound_units=answer.upper_bound_units + 1)
    monkeypatch.setattr(
        "eve_courier_optimizer.solver.solve_batches", lambda *args, **kwargs: partial
    )
    monkeypatch.setattr(
        "eve_courier_optimizer.solver.solve_system_relaxation_master",
        lambda *args, **kwargs: SystemRelaxationBound("UNKNOWN", None, None, 0.0, 0, 0, 2),
    )
    result = _run_dense_decomposition(p, tiny_graph, SolverConfig())
    assert result.simulation is not None and result.simulation.report.valid
    assert (
        result.relaxation is not None
        and result.relaxation.upper_bound_units == partial.upper_bound_units
    )
    assert result.learned_reward_cuts[0].upper_bound_units == partial.upper_bound_units
    assert not result.proven_infeasible and result.status_name == "master_unknown"
