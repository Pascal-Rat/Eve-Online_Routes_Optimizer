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

    This intentionally simple implementation is independent of OR-Tools. It stores one bit per
    contract in two integers: a set of contracts ever picked up and a set already delivered. For
    an identical ``(system, picked set, delivered set)``, an earlier arrival is always at least as
    useful as a later one because every remaining time constraint is an upper bound. Exploring
    every earliest-arrival state therefore proves the optimum for these small instances.
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

    contract_count = len(contracts)
    cargo_volume_by_index = [contract.contract.volume_units for contract in contracts]
    collateral_by_index = [contract.contract.collateral_units for contract in contracts]
    reward_by_index = [contract.contract.reward_units for contract in contracts]
    deadline_seconds_by_index = [
        contract.contract.days_to_complete * 86_400 for contract in contracts
    ]
    all_contracts_mask = (1 << contract_count) - 1

    def sum_selected_values(values: list[int], selection_mask: int) -> int:
        return sum(
            values[contract_index]
            for contract_index in range(contract_count)
            if selection_mask & (1 << contract_index)
        )

    # Queue fields: elapsed seconds, stable tie-breaker, system, picked mask, delivered mask.
    next_queue_order = 0
    states_to_explore: list[tuple[int, int, int, int, int]] = [
        (0, next_queue_order, constraints.start_system_id, 0, 0)
    ]
    earliest_arrival_by_state: dict[tuple[int, int, int], int] = {
        (constraints.start_system_id, 0, 0): 0
    }
    best_reward_units = 0
    explored_state_count = 0

    def can_reach_required_finish(
        current_system_id: int,
        elapsed_seconds: int,
    ) -> bool:
        terminal_system_id = constraints.terminal_system_id
        if terminal_system_id is None:
            return True
        jump_count = prepared.jump_matrix.get((current_system_id, terminal_system_id))
        return (
            jump_count is not None
            and elapsed_seconds + jump_count * constraints.travel.seconds_per_jump
            <= constraints.horizon_seconds
        )

    while states_to_explore:
        (
            elapsed_seconds,
            _,
            current_system_id,
            picked_contracts_mask,
            delivered_contracts_mask,
        ) = heapq.heappop(states_to_explore)
        state_key = (
            current_system_id,
            picked_contracts_mask,
            delivered_contracts_mask,
        )
        if earliest_arrival_by_state.get(state_key) != elapsed_seconds:
            continue
        explored_state_count += 1
        if picked_contracts_mask == delivered_contracts_mask and can_reach_required_finish(
            current_system_id, elapsed_seconds
        ):
            best_reward_units = max(
                best_reward_units,
                sum_selected_values(reward_by_index, delivered_contracts_mask),
            )

        carried_contracts_mask = picked_contracts_mask & ~delivered_contracts_mask
        cargo_load_units = sum_selected_values(cargo_volume_by_index, carried_contracts_mask)
        locked_collateral_units = sum_selected_values(collateral_by_index, picked_contracts_mask)

        unpicked_contracts_mask = all_contracts_mask & ~picked_contracts_mask
        for contract_index in range(contract_count):
            contract_bit = 1 << contract_index
            if not (unpicked_contracts_mask & contract_bit):
                continue
            if (
                constraints.max_simultaneous_contracts is not None
                and carried_contracts_mask.bit_count() >= constraints.max_simultaneous_contracts
            ):
                continue
            if (
                cargo_load_units + cargo_volume_by_index[contract_index]
                > constraints.cargo_capacity_units
            ):
                continue
            if (
                locked_collateral_units + collateral_by_index[contract_index]
                > constraints.collateral_budget_units
            ):
                continue
            pickup_system_id = contracts[contract_index].origin_system_id
            jump_count = prepared.jump_matrix.get((current_system_id, pickup_system_id))
            if jump_count is None:
                continue
            pickup_completion_seconds = (
                elapsed_seconds
                + jump_count * constraints.travel.seconds_per_jump
                + constraints.travel.service_seconds
            )
            if pickup_completion_seconds > constraints.horizon_seconds:
                continue
            next_state_key = (
                pickup_system_id,
                picked_contracts_mask | contract_bit,
                delivered_contracts_mask,
            )
            if pickup_completion_seconds < earliest_arrival_by_state.get(next_state_key, 2**63 - 1):
                earliest_arrival_by_state[next_state_key] = pickup_completion_seconds
                next_queue_order += 1
                heapq.heappush(
                    states_to_explore,
                    (
                        pickup_completion_seconds,
                        next_queue_order,
                        pickup_system_id,
                        picked_contracts_mask | contract_bit,
                        delivered_contracts_mask,
                    ),
                )

        for contract_index in range(contract_count):
            contract_bit = 1 << contract_index
            if not (carried_contracts_mask & contract_bit):
                continue
            delivery_system_id = contracts[contract_index].destination_system_id
            jump_count = prepared.jump_matrix.get((current_system_id, delivery_system_id))
            if jump_count is None:
                continue
            delivery_completion_seconds = (
                elapsed_seconds
                + jump_count * constraints.travel.seconds_per_jump
                + constraints.travel.service_seconds
            )
            if (
                delivery_completion_seconds > constraints.horizon_seconds
                or delivery_completion_seconds > deadline_seconds_by_index[contract_index]
            ):
                continue
            next_state_key = (
                delivery_system_id,
                picked_contracts_mask,
                delivered_contracts_mask | contract_bit,
            )
            if delivery_completion_seconds < earliest_arrival_by_state.get(
                next_state_key, 2**63 - 1
            ):
                earliest_arrival_by_state[next_state_key] = delivery_completion_seconds
                next_queue_order += 1
                heapq.heappush(
                    states_to_explore,
                    (
                        delivery_completion_seconds,
                        next_queue_order,
                        delivery_system_id,
                        picked_contracts_mask,
                        delivered_contracts_mask | contract_bit,
                    ),
                )

    return ReferenceResult(
        objective_units=best_reward_units,
        explored_states=explored_state_count,
    )
