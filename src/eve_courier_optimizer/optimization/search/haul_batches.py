"""Exact compact batching for a shared haul lane with nonbinding time windows."""

from __future__ import annotations

import math
import time

from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import ActionKind, CollateralMode, PlannedAction
from eve_courier_optimizer.optimization.models.selection_bounds import (
    RewardBoundRecorder,
    integer_upper_bound,
)
from eve_courier_optimizer.optimization.search.subset_search import SubsetSearchResult
from eve_courier_optimizer.routing.route_problem import RouteProblem


def solve_batches(
    problem: RouteProblem, *, max_time_seconds: float, random_seed: int = 0
) -> SubsetSearchResult | None:
    """Return None outside the exact batch normal form; preserve the full optional pool.

    With one distinct origin/destination pair, locked collateral and nonbinding deadlines,
    unloading every carried item on arrival can only improve resources. All useful travel can
    therefore be arranged as origin-to-destination batches. Neither visit counts nor jobs are
    truncated: the trip bound follows from the horizon, approach and required finish distances.
    Interchangeable batches may be sorted by reward only because no time window binds.
    """
    if not math.isfinite(max_time_seconds) or max_time_seconds <= 0:
        raise ValueError("max_time_seconds must be finite and positive")
    started = time.perf_counter()
    c = problem.constraints
    items = problem.contracts
    lanes = {(i.origin_system_id, i.destination_system_id) for i in items}
    if (
        not items
        or len(lanes) != 1
        or problem.active_shipments
        or c.required_system_ids
        or c.collateral_mode is not CollateralMode.LOCKED
        or any(i.days_to_complete * 86_400 < c.horizon_seconds for i in items)
    ):
        return None
    origin, destination = next(iter(lanes))
    if origin == destination:
        return None

    def travel(source: int, target: int | None) -> int | None:
        if target is None:
            return 0
        jumps = problem.jump_matrix.get((source, target))
        return None if jumps is None else jumps * c.travel.seconds_per_jump

    approach = travel(c.start_system_id, origin)
    outward = travel(origin, destination)
    back = travel(destination, origin)
    leave = travel(destination, c.terminal_system_id)
    empty = travel(c.start_system_id, c.terminal_system_id)
    if approach is None or outward is None or back is None or leave is None or empty is None:
        return None
    if outward + back <= 0 or empty > c.horizon_seconds:
        return None
    trips = min(
        len(items), max(0, (c.horizon_seconds - approach - leave + back) // (outward + back))
    )
    # Avoid replacing one large materialized model with an even larger assignment model.
    if len(items) * trips > 250_000:
        return None

    model = cp_model.CpModel()
    used = [model.new_bool_var(f"batch_{b}") for b in range(trips)]
    selected = [model.new_bool_var(f"selected_{i.contract_id}") for i in items]
    assigned = [
        [model.new_bool_var(f"job_{i}_batch_{b}") for b in range(trips)] for i in range(len(items))
    ]
    for i, variable in enumerate(selected):
        model.add(sum(assigned[i]) == variable)
    batch_rewards = []
    for b in range(trips):
        count = sum(assigned[i][b] for i in range(len(items)))
        model.add(count >= used[b])
        model.add(count <= len(items) * used[b])
        model.add(
            sum(item.volume_units * assigned[i][b] for i, item in enumerate(items))
            <= c.cargo_capacity_units
        )
        if c.max_simultaneous_contracts is not None:
            model.add(count <= c.max_simultaneous_contracts)
        batch_rewards.append(
            sum(item.reward_units * assigned[i][b] for i, item in enumerate(items))
        )
        if b:
            model.add(used[b] <= used[b - 1])
            model.add(batch_rewards[b] <= batch_rewards[b - 1])
    model.add(
        sum(item.collateral_units * selected[i] for i, item in enumerate(items))
        <= c.collateral_budget_units
    )
    if trips:
        model.add(
            (approach + leave - back) * used[0]
            + (outward + back) * sum(used)
            + 2 * c.travel.service_seconds * sum(selected)
            <= c.horizon_seconds
        )
    reward = sum(item.reward_units * selected[i] for i, item in enumerate(items))
    model.maximize(reward)
    remaining = max_time_seconds - (time.perf_counter() - started)
    if model.validate() or remaining <= 0:
        return SubsetSearchResult(None, None, (), (), False, 0, time.perf_counter() - started)
    cp = cp_model.CpSolver()
    cp.parameters.max_time_in_seconds = remaining
    cp.parameters.num_search_workers = 1
    cp.parameters.random_seed = random_seed
    bounds = RewardBoundRecorder()
    cp.best_bound_callback = bounds
    status = cp.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SubsetSearchResult(
            None,
            bounds.upper_bound_units if status == cp_model.UNKNOWN else None,
            (),
            (),
            False,
            0,
            time.perf_counter() - started,
        )
    visits: list[PlannedAction] = []
    ids = []
    for b in range(trips):
        batch = sorted(item.contract_id for i, item in enumerate(items) if cp.value(assigned[i][b]))
        ids.extend(batch)
        visits.extend(PlannedAction(ActionKind.PICKUP, cid) for cid in batch)
        visits.extend(PlannedAction(ActionKind.DELIVERY, cid) for cid in batch)
    objective = int(cp.value(reward))
    upper = (
        objective
        if status == cp_model.OPTIMAL
        else max(objective, integer_upper_bound(cp.best_objective_bound))
    )
    return SubsetSearchResult(
        objective,
        upper,
        tuple(visits),
        tuple(sorted(ids)),
        status == cp_model.OPTIMAL,
        0,
        time.perf_counter() - started,
        cp_branches=cp.num_branches,
        cp_conflicts=cp.num_conflicts,
    )
