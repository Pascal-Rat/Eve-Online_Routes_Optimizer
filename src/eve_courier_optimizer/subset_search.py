"""Bounded exact search on small contract subsets, without event-pair decision variables.

Keep this implementation separate from the exhaustive reference solver: the latter remains an
independent check on production search. Only a completed search supplies a subset reward ceiling.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass

from .domain import ActionKind, CollateralMode, PlannedAction
from .planning import PreparedProblem


@dataclass(frozen=True, slots=True)
class SubsetSearchResult:
    objective_units: int | None
    upper_bound_units: int | None
    visits: tuple[PlannedAction, ...]
    selected_contract_ids: tuple[int, ...]
    complete: bool
    explored_states: int
    wall_time_seconds: float
    infeasible_core_ids: tuple[int, ...] = ()
    cp_branches: int = 0
    cp_conflicts: int = 0


def solve_subset(
    prepared: PreparedProblem,
    *,
    max_time_seconds: float,
    contract_limit: int = 14,
    max_states: int = 250_000,
) -> SubsetSearchResult | None:
    """Optimize an optional subset, or return None when its structure is unsupported.

    An earlier arrival dominates for identical system/picked/delivered sets when all deadlines
    are absolute. Rolling acceptance is also supported when completion windows cannot bind:
    listing expiry is absolute and locked funds depend only on the carried set. Binding rolling
    completion windows require pickup-time state and are deliberately left to the general model.

    Every child retains an immutable parent label, including when a state later receives an
    earlier arrival. This makes reconstructed routes consistent with their recorded actions.
    State and time limits discard no possibilities silently: an interrupted search has no bound.
    """
    if not math.isfinite(max_time_seconds) or max_time_seconds <= 0:
        raise ValueError("max_time_seconds must be finite and positive")
    if max_states < 1 or contract_limit < 0:
        raise ValueError("max_states must be positive and contract_limit non-negative")
    started = time.perf_counter()
    problem = prepared.problem
    c = problem.constraints
    items = problem.contracts
    count = len(items)
    rolling = c.collateral_mode is CollateralMode.ROLLING
    if (
        count > contract_limit
        or problem.active_shipments
        or c.required_system_ids
        or (rolling and any(i.days_to_complete * 86_400 < c.horizon_seconds for i in items))
    ):
        return None

    horizon = c.horizon_seconds
    service = c.travel.service_seconds
    unreachable = horizon + 1

    def travel(source: int, destination: int) -> int:
        jumps = prepared.jump_matrix.get((source, destination))
        return unreachable if jumps is None else jumps * c.travel.seconds_per_jump

    systems = tuple(
        sorted(
            {c.start_system_id}
            | {s for i in items for s in (i.origin_system_id, i.destination_system_id)}
        )
    )
    system_index = {system: index for index, system in enumerate(systems)}
    start_system = system_index[c.start_system_id]
    distance = tuple(tuple(travel(a, b) for b in systems) for a in systems)
    finish_cost = tuple(
        0 if c.terminal_system_id is None else travel(s, c.terminal_system_id) for s in systems
    )
    pickups = tuple(system_index[i.origin_system_id] for i in items)
    deliveries = tuple(system_index[i.destination_system_id] for i in items)
    volume = tuple(i.volume_units for i in items)
    collateral = tuple(i.collateral_units for i in items)
    rewards = tuple(i.reward_units for i in items)
    deadlines = tuple(i.days_to_complete * 86_400 for i in items)
    pickup_limits = tuple(contract.last_pickup_second(c.snapshot_time) for contract in items)

    all_mask = (1 << count) - 1
    volume_sum = [0] * (all_mask + 1)
    collateral_sum = [0] * (all_mask + 1)
    reward_sum = [0] * (all_mask + 1)
    # Label fields: current system index, picked mask, delivered mask, parent label, action.
    labels = [(start_system, 0, 0, -1, -1)]
    queue = [(0, 0)]
    earliest = {start_system: 0}
    best_label = 0 if finish_cost[start_system] <= horizon else None
    best_reward = 0 if best_label is not None else None
    explored = 0
    complete = False
    feasible_masks: set[int] = set()

    def result() -> SubsetSearchResult:
        actions = []
        label = best_label
        while label is not None and label != 0:
            _, _, _, parent, action = labels[label]
            index = action % count
            actions.append(
                PlannedAction(
                    ActionKind.PICKUP if action < count else ActionKind.DELIVERY,
                    items[index].contract_id,
                )
            )
            label = parent
        chosen = 0 if best_label is None else labels[best_label][2]
        core = 0
        if complete and best_reward is not None and best_reward < reward_sum[all_mask]:
            # A completed search enumerated every feasible completion mask. Downward closure
            # makes membership a sufficient exact deletion test, with no repeated oracle solves.
            core = all_mask
            for i in range(count):
                trial = core & ~(1 << i)
                if trial not in feasible_masks:
                    core = trial
        return SubsetSearchResult(
            best_reward,
            best_reward if complete else None,
            tuple(reversed(actions)),
            tuple(sorted(items[i].contract_id for i in range(count) if chosen & (1 << i))),
            complete,
            explored,
            time.perf_counter() - started,
            tuple(sorted(items[i].contract_id for i in range(count) if core & (1 << i))),
        )

    for mask in range(1, all_mask + 1):
        if mask % 1024 == 0 and time.perf_counter() - started >= max_time_seconds:
            return result()
        bit = mask & -mask
        index = bit.bit_length() - 1
        previous = mask ^ bit
        volume_sum[mask] = volume_sum[previous] + volume[index]
        collateral_sum[mask] = collateral_sum[previous] + collateral[index]
        reward_sum[mask] = reward_sum[previous] + rewards[index]

    popped = 0
    while queue:
        if popped % 64 == 0 and time.perf_counter() - started >= max_time_seconds:
            return result()
        popped += 1
        elapsed, label_id = heapq.heappop(queue)
        current, picked, delivered, _, _ = labels[label_id]
        key = ((picked << count) | delivered) * len(systems) + current
        if earliest[key] != elapsed:
            continue
        explored += 1
        if elapsed + finish_cost[current] > horizon:
            continue
        if picked == delivered:
            feasible_masks.add(delivered)
            if best_reward is None or reward_sum[delivered] > best_reward:
                best_reward, best_label = reward_sum[delivered], label_id
        if best_reward == reward_sum[all_mask]:
            complete = True  # Non-negative rewards make this a matching trivial upper bound.
            return result()
        carried = picked ^ delivered
        locked = collateral_sum[carried if rolling else picked]
        parcel_room = (
            c.max_simultaneous_contracts is None
            or carried.bit_count() < c.max_simultaneous_contracts
        )
        for pickup in (True, False):
            remaining = (all_mask ^ picked) if pickup else carried
            while remaining:
                bit = remaining & -remaining
                remaining ^= bit
                index = bit.bit_length() - 1
                destination = pickups[index] if pickup else deliveries[index]
                arrival = elapsed + distance[current][destination]
                completion = arrival + service
                if completion + finish_cost[destination] > horizon:
                    continue
                if pickup:
                    if (
                        not parcel_room
                        or volume_sum[carried] + volume[index] > c.cargo_capacity_units
                        or locked + collateral[index] > c.collateral_budget_units
                        or (rolling and arrival > pickup_limits[index])
                    ):
                        continue
                    earliest_delivery = (
                        completion + distance[destination][deliveries[index]] + service
                    )
                    if earliest_delivery + finish_cost[deliveries[index]] > horizon or (
                        not rolling and earliest_delivery > deadlines[index]
                    ):
                        continue
                    next_picked, next_delivered = picked | bit, delivered
                else:
                    if not rolling and completion > deadlines[index]:
                        continue
                    next_picked, next_delivered = picked, delivered | bit
                next_key = ((next_picked << count) | next_delivered) * len(systems) + destination
                if completion >= earliest.get(next_key, unreachable):
                    continue
                if len(labels) >= max_states:
                    return result()
                earliest[next_key] = completion
                next_label = len(labels)
                labels.append(
                    (
                        destination,
                        next_picked,
                        next_delivered,
                        label_id,
                        index if pickup else count + index,
                    )
                )
                heapq.heappush(queue, (completion, next_label))
    complete = True
    return result()
