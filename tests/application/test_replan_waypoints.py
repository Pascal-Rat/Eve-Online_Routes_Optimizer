from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.planner import CourierPlanner
from eve_courier_optimizer.domain import (
    ActiveShipment,
    CollateralMode,
    RoutableContract,
    SecurityPolicy,
    TravelTimeModel,
)
from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.optimization import SolverConfig
from eve_courier_optimizer.optimization.models.pickup_delivery import PickupDeliveryModel
from eve_courier_optimizer.optimization.models.system_tour import SystemTourModel
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.route_replay import VerifiedRoute
from tests.support.scenarios import make_contract, make_snapshot


@pytest.mark.parametrize("required", [frozenset({1}), frozenset({3}), frozenset({1, 2, 3})])
@pytest.mark.parametrize("terminal", [None, 1, 3])
def test_replan_handles_required_start_terminal_and_active_delivery(
    now: datetime, tiny_graph: UniverseGraph, required: frozenset[int], terminal: int | None
) -> None:
    active = ActiveShipment(
        RoutableContract.resolve(make_contract(now, 1, 101, 102), 1, 2),
        now + timedelta(days=1),
        True,
    )
    trip = CourierTrip(
        now,
        now + timedelta(seconds=300),
        1,
        10,
        300,
        CollateralMode.ROLLING,
        TravelTimeModel(60, 0),
        SecurityPolicy(),
        terminal_system_id=terminal,
        remaining_required_system_ids=required,
        active_shipments=(active,),
    )
    snapshot = make_snapshot(now, make_contract(now, 2, 101, 103))
    plan = CourierPlanner(tiny_graph, EsiClient()).replan(
        snapshot, trip, at=now, solver_config=SolverConfig(max_time_seconds=2)
    )
    assert plan.result.certificate.feasibility_verified
    assert any(step.contract_id == 1 for step in plan.result.route)


def test_models_reject_incumbents_verified_for_a_different_problem(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    from dataclasses import replace

    from eve_courier_optimizer.routing.route_problem import RouteProblem
    from tests.support.scenarios import reward_constraints as constraints

    problem = RouteProblem.from_snapshot(make_snapshot(now), tiny_graph, constraints(now))
    incumbent = VerifiedRoute.verify(problem, tiny_graph, (), ())
    changed = replace(problem, constraints=replace(problem.constraints, horizon_seconds=1))
    for model in (PickupDeliveryModel(changed, selection_hint=()), SystemTourModel(changed)):
        with pytest.raises(ValueError, match="different route problem"):
            model.install_incumbent(incumbent)
