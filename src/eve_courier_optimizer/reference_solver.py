"""Small exhaustive dynamic-programming solver used as an independent correctness oracle."""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from .domain import CollateralMode
from .planning import PreparedProblem


@dataclass(frozen=True, slots=True)
class ReferenceResult:
    objective_units: int
    explored_states: int


def solve_reference(prepared: PreparedProblem, *, contract_limit: int = 12) -> ReferenceResult:
    """Exhaustively solve a small locked-collateral instance.

    State is ``(current_system, ever_picked_mask, delivered_mask)``. For identical state, an
    earlier arrival dominates every later arrival because all remaining constraints are upper
    time bounds. Enumerating every non-dominated transition therefore proves the optimum.
    """

    problem = prepared.problem
    constraints = problem.constraints
    contracts = problem.contracts
    if constraints.collateral_mode is not CollateralMode.LOCKED:
        raise ValueError("reference solver currently supports locked collateral only")
    if problem.active_shipments:
        raise ValueError("reference solver currently requires no active shipments")
    if constraints.required_system_ids:
        raise ValueError("reference solver currently does not support required waypoint systems")
    if len(contracts) > contract_limit:
        raise ValueError(f"reference solver limit is {contract_limit} contracts")

    n = len(contracts)
    volume = [item.contract.volume_units for item in contracts]
    collateral = [item.contract.collateral_units for item in contracts]
    reward = [item.contract.reward_units for item in contracts]
    deadlines = [item.contract.days_to_complete * 86_400 for item in contracts]
    full_mask = (1 << n) - 1

    def sum_mask(values: list[int], mask: int) -> int:
        return sum(values[index] for index in range(n) if mask & (1 << index))

    # Heap fields: elapsed, tie_breaker, system, picked, delivered.
    serial = 0
    heap: list[tuple[int, int, int, int, int]] = [
        (0, serial, constraints.start_system_id, 0, 0)
    ]
    best_time: dict[tuple[int, int, int], int] = {
        (constraints.start_system_id, 0, 0): 0
    }
    best_reward = 0
    explored = 0

    def can_finish(current_system: int, elapsed: int) -> bool:
        terminal = constraints.terminal_system_id
        if terminal is None:
            return True
        jumps = prepared.jump_matrix.get((current_system, terminal))
        return (
            jumps is not None
            and elapsed + jumps * constraints.travel.seconds_per_jump
            <= constraints.horizon_seconds
        )

    while heap:
        elapsed, _, current_system, picked, delivered = heapq.heappop(heap)
        key = (current_system, picked, delivered)
        if best_time.get(key) != elapsed:
            continue
        explored += 1
        if picked == delivered and can_finish(current_system, elapsed):
            best_reward = max(best_reward, sum_mask(reward, delivered))

        active_mask = picked & ~delivered
        cargo = sum_mask(volume, active_mask)
        locked_collateral = sum_mask(collateral, picked)

        unpicked = full_mask & ~picked
        for index in range(n):
            bit = 1 << index
            if not (unpicked & bit):
                continue
            if (
                constraints.max_simultaneous_contracts is not None
                and active_mask.bit_count() >= constraints.max_simultaneous_contracts
            ):
                continue
            if cargo + volume[index] > constraints.cargo_capacity_units:
                continue
            if locked_collateral + collateral[index] > constraints.collateral_budget_units:
                continue
            target = contracts[index].origin_system_id
            jumps = prepared.jump_matrix.get((current_system, target))
            if jumps is None:
                continue
            next_time = (
                elapsed
                + jumps * constraints.travel.seconds_per_jump
                + constraints.travel.service_seconds
            )
            if next_time > constraints.horizon_seconds:
                continue
            next_key = (target, picked | bit, delivered)
            if next_time < best_time.get(next_key, 2**63 - 1):
                best_time[next_key] = next_time
                serial += 1
                heapq.heappush(heap, (next_time, serial, target, picked | bit, delivered))

        for index in range(n):
            bit = 1 << index
            if not (active_mask & bit):
                continue
            target = contracts[index].destination_system_id
            jumps = prepared.jump_matrix.get((current_system, target))
            if jumps is None:
                continue
            next_time = (
                elapsed
                + jumps * constraints.travel.seconds_per_jump
                + constraints.travel.service_seconds
            )
            if next_time > constraints.horizon_seconds or next_time > deadlines[index]:
                continue
            next_key = (target, picked, delivered | bit)
            if next_time < best_time.get(next_key, 2**63 - 1):
                best_time[next_key] = next_time
                serial += 1
                heapq.heappush(heap, (next_time, serial, target, picked, delivered | bit))

    return ReferenceResult(objective_units=best_reward, explored_states=explored)
