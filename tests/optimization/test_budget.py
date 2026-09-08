from __future__ import annotations

from datetime import datetime
from typing import Never

import pytest

from eve_courier_optimizer.domain import PlannedVisit, ProofStatus
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.optimization.models.pickup_delivery import PickupDeliveryModel
from eve_courier_optimizer.optimization.models.selection_bounds import SelectionCuts
from eve_courier_optimizer.optimization.search.route_insertion import construct_incumbent
from eve_courier_optimizer.optimization.solver_config import SearchBudget
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.exhaustive_optimum import solve_exhaustively
from eve_courier_optimizer.verification.route_replay import VerifiedRoute
from tests.conftest import make_contract, make_snapshot
from tests.optimization.test_optimizer import constraints


def test_budget_uses_one_deadline_and_caps_each_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("time.perf_counter", lambda: clock[0])
    budget = SearchBudget(10)
    assert budget.remaining(20) == 10
    clock[0] += 7
    assert budget.remaining(20) == 3
    assert budget.remaining(2) == 2
    clock[0] += 4
    assert budget.expired() and budget.remaining() == 0
    assert SearchBudget(None).remaining(20) == 20


def test_expiration_after_construction_keeps_incumbent_and_skips_later_search(
    now: datetime, tiny_graph: UniverseGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *(make_contract(now, i, 101, 103) for i in range(1, 21))),
        tiny_graph,
        constraints(now, horizon=50, collateral=100),
    )
    incumbent = construct_incumbent(problem, tiny_graph)
    assert incumbent is not None
    clock = [100.0]
    monkeypatch.setattr("time.perf_counter", lambda: clock[0])

    def construct(p: RouteProblem, g: UniverseGraph, *, deadline: float) -> VerifiedRoute:
        assert deadline == 110
        clock[0] = 111
        return incumbent

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("an expired solve started another search phase")

    monkeypatch.setattr(
        "eve_courier_optimizer.optimization.search.route_insertion.construct_incumbent", construct
    )
    monkeypatch.setattr(
        "eve_courier_optimizer.optimization.models.selection_bounds.build_selection_cuts", forbidden
    )
    monkeypatch.setattr(
        "eve_courier_optimizer.optimization.models.pickup_delivery.PickupDeliveryModel", forbidden
    )
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    assert result.total_reward_units == incumbent.simulation.total_reward_units
    assert result.certificate.feasibility_verified
    assert result.certificate.status is ProofStatus.FEASIBLE_NOT_PROVEN
    assert result.certificate.solver_status == "TIME_LIMIT"


def test_expired_independent_reference_does_not_claim_completion(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    assert not solve_exhaustively(problem, deadline=0).complete


def test_model_construction_consumes_the_remaining_search_allowance(
    now: datetime, tiny_graph: UniverseGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *(make_contract(now, cid, 101, 103) for cid in (1, 2))),
        tiny_graph,
        constraints(now, collateral=100),
    )
    clock = [100.0]
    monkeypatch.setattr("time.perf_counter", lambda: clock[0])

    def build(
        p: RouteProblem,
        *,
        selection_hint: tuple[PlannedVisit, ...],
        selection_cuts: SelectionCuts,
    ) -> PickupDeliveryModel:
        model = PickupDeliveryModel(p, selection_hint=selection_hint, selection_cuts=selection_cuts)
        clock[0] = 111
        return model

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("CP-SAT started after model construction exhausted the deadline")

    monkeypatch.setattr(
        "eve_courier_optimizer.optimization.models.pickup_delivery.PickupDeliveryModel", build
    )
    monkeypatch.setattr(SolverConfig, "solver", forbidden)
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    assert result.certificate.status is ProofStatus.FEASIBLE_NOT_PROVEN
    assert result.certificate.solver_status == "TIME_LIMIT"
    assert result.certificate.feasibility_verified and result.total_reward_units == 500


def test_trivial_reward_proof_does_not_claim_decomposition_ran(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    result = RouteOptimizer(
        problem,
        tiny_graph,
        config=SolverConfig(minimize_finish_time_after_proof=False, independent_reference_limit=0),
    ).solve()
    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.certificate.solver_status == "TRIVIAL_BOUND_MATCHED"
    assert not result.certificate.decomposition_proof_closed
    assert result.certificate.system_relaxation_status is None
