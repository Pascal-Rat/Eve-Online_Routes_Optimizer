from datetime import datetime

from eve_courier_optimizer.domain import PlanningConstraints, TravelTimeModel
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.exhaustive_optimum import solve_exhaustively
from tests.conftest import make_snapshot


def test_zero_reward_and_infeasible_finish_are_distinct(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    def problem(horizon: int) -> RouteProblem:
        return RouteProblem.from_snapshot(
            make_snapshot(now),
            tiny_graph,
            PlanningConstraints(
                start_system_id=1,
                finish_system_id=3,
                return_to_start=False,
                cargo_capacity_units=0,
                collateral_budget_units=0,
                horizon_seconds=horizon,
                snapshot_time=now,
                travel=TravelTimeModel(10, 0),
            ),
        )

    assert solve_exhaustively(problem(20)).objective_units == 0
    assert solve_exhaustively(problem(19)).objective_units is None
