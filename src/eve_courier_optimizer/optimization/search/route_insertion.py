"""Constructive route improvements used only as independently verified search incumbents."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from itertools import combinations

from eve_courier_optimizer.domain import (
    ActionKind,
    CollateralMode,
    PlannedAction,
    PlannedVisit,
    PlannedWaypoint,
    RoutableContract,
)
from eve_courier_optimizer.routing.route_problem import RouteProblem, SingleContractScore
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.route_replay import (
    SimulationResult,
    VerifiedRoute,
    simulate_and_verify,
)


def insert_additional_contracts(
    problem: RouteProblem,
    graph: UniverseGraph,
    visits: tuple[PlannedVisit, ...],
    simulation: SimulationResult,
    *,
    candidate_order: tuple[SingleContractScore, ...] | None = None,
) -> tuple[tuple[PlannedVisit, ...], SimulationResult]:
    """Try each unused contract in every precedence-respecting pair of insertion positions.

    Existing actions retain their order. Cheap distance and interval-load checks rank insertions;
    the independent simulator decides acceptance, including deadlines and route requirements.
    This adds shared-haul opportunities to a sequential seed without committing the exact search
    to its choices. Active commitments currently rely on the exact constructor instead.
    """
    if not simulation.report.valid or problem.active_shipments:
        return visits, simulation
    c = problem.constraints
    contracts = {contract.contract_id: contract for contract in problem.contracts}
    selected = {visit.contract_id for visit in visits if isinstance(visit, PlannedAction)}
    candidates = (
        candidate_order
        if candidate_order is not None
        else sorted(
            problem.scores,
            key=lambda score: (
                score.reward_per_hour_isk,
                score.contract.reward_units,
                -score.contract.contract_id,
            ),
            reverse=True,
        )
    )

    def distance(source: int, destination: int | None) -> int:
        if destination is None:
            return 0
        jumps = problem.jump_matrix.get((source, destination))
        # Unreachable insertions cannot beat the horizon; avoid special arithmetic at each slot.
        return c.horizon_seconds + 1 if jumps is None else jumps * c.travel.seconds_per_jump

    for score in candidates:
        contract = score.contract
        if contract.contract_id in selected:
            continue
        if simulation.finish_seconds + 2 * c.travel.service_seconds > c.horizon_seconds:
            break
        if c.collateral_mode is CollateralMode.LOCKED and (
            sum(contracts[i].collateral_units for i in selected) + contract.collateral_units
            > c.collateral_budget_units
        ):
            continue
        systems = [c.start_system_id]
        cargo, parcels, collateral = [0], [0], [0]
        for visit in visits:
            load, count, locked = cargo[-1], parcels[-1], collateral[-1]
            if isinstance(visit, PlannedAction):
                existing = contracts[visit.contract_id]
                pickup = visit.action is ActionKind.PICKUP
                sign = 1 if pickup else -1
                systems.append(
                    existing.origin_system_id if pickup else existing.destination_system_id
                )
                load += sign * existing.volume_units
                count += sign
                locked += sign * existing.collateral_units
            else:
                systems.append(visit.system_id)
            cargo.append(load)
            parcels.append(count)
            collateral.append(locked)
        endpoints: list[int | None] = [*systems[1:], c.terminal_system_id]
        origin, destination = contract.origin_system_id, contract.destination_system_id
        options: list[tuple[int, int, int]] = []
        for pickup_slot in range(len(systems)):
            max_cargo = max_parcels = max_collateral = 0
            for delivery_slot in range(pickup_slot, len(systems)):
                max_cargo = max(max_cargo, cargo[delivery_slot])
                max_parcels = max(max_parcels, parcels[delivery_slot])
                max_collateral = max(max_collateral, collateral[delivery_slot])
                if (
                    max_cargo + contract.volume_units > c.cargo_capacity_units
                    or (
                        c.max_simultaneous_contracts is not None
                        and max_parcels + 1 > c.max_simultaneous_contracts
                    )
                    or (
                        c.collateral_mode is CollateralMode.ROLLING
                        and max_collateral + contract.collateral_units > c.collateral_budget_units
                    )
                ):
                    break  # Extending this carrying interval cannot reduce its maximum load.
                before, after = systems[pickup_slot], endpoints[pickup_slot]
                if pickup_slot == delivery_slot:
                    extra = (
                        distance(before, origin)
                        + distance(origin, destination)
                        + distance(destination, after)
                        - distance(before, after)
                    )
                else:
                    before_d, after_d = systems[delivery_slot], endpoints[delivery_slot]
                    extra = (
                        distance(before, origin)
                        + distance(origin, after)
                        - distance(before, after)
                        + distance(before_d, destination)
                        + distance(destination, after_d)
                        - distance(before_d, after_d)
                    )
                finish = simulation.finish_seconds + extra + 2 * c.travel.service_seconds
                if finish <= c.horizon_seconds:
                    options.append((finish, pickup_slot, delivery_slot))
        for _, pickup_slot, delivery_slot in sorted(options):
            trial = (
                *visits[:pickup_slot],
                PlannedAction(ActionKind.PICKUP, contract.contract_id),
                *visits[pickup_slot:delivery_slot],
                PlannedAction(ActionKind.DELIVERY, contract.contract_id),
                *visits[delivery_slot:],
            )
            trial_simulation = simulate_and_verify(
                problem, graph, trial, tuple(sorted(selected | {contract.contract_id}))
            )
            if trial_simulation.report.valid:
                visits, simulation = trial, trial_simulation
                selected.add(contract.contract_id)
                break
    return visits, simulation


def _insertion_orders(problem: RouteProblem) -> tuple[tuple[SingleContractScore, ...], ...]:
    return (
        tuple(
            sorted(
                problem.scores,
                key=lambda s: (
                    -s.reward_per_hour_isk,
                    -s.contract.reward_units,
                    s.contract.contract_id,
                ),
            )
        ),
        tuple(
            sorted(
                problem.scores,
                key=lambda s: (
                    -s.contract.reward_units,
                    s.contract.contract_id,
                ),
            )
        ),
        tuple(
            sorted(
                problem.scores,
                key=lambda s: (
                    -s.contract.reward_units / max(1, s.contract.collateral_units),
                    s.contract.contract_id,
                ),
            )
        ),
    )


def _remove_contract_visits(
    visits: tuple[PlannedVisit, ...],
    removed: frozenset[int],
    contracts: Mapping[int, RoutableContract],
    required_system_ids: frozenset[int],
) -> tuple[PlannedVisit, ...]:
    remaining: list[PlannedVisit] = []
    for visit in visits:
        if isinstance(visit, PlannedAction) and visit.contract_id in removed:
            contract = contracts[visit.contract_id]
            system = (
                contract.origin_system_id
                if visit.action is ActionKind.PICKUP
                else contract.destination_system_id
            )
            if system in required_system_ids:
                remaining.append(PlannedWaypoint(system))
        else:
            remaining.append(visit)
    return tuple(remaining)


def improve_incumbent(
    problem: RouteProblem,
    graph: UniverseGraph,
    visits: tuple[PlannedVisit, ...],
    simulation: SimulationResult,
    *,
    restart_visits: tuple[PlannedVisit, ...],
) -> tuple[tuple[PlannedVisit, ...], SimulationResult]:
    """Rebuild in three orders, then explore one contract-removal/repair neighborhood.

    A sequential seed can spend capacity or collateral on an early attractive job that blocks
    better shared hauls. Rebuilding from a mandatory-waypoint route can replace those choices.
    Each removal starts from the same best rebuilt route, so this is a bounded pass rather than
    an unbounded local search. Only independently verified improvements become search hints and
    lower bounds; candidate eligibility, objective coefficients and proof scope remain unchanged.
    """
    best = insert_additional_contracts(problem, graph, visits, simulation)
    if problem.active_shipments or not best[1].report.valid:
        return best
    restart_simulation = simulate_and_verify(problem, graph, restart_visits, ())
    if not restart_simulation.report.valid:
        return best
    orders = _insertion_orders(problem)

    def quality(result: tuple[tuple[PlannedVisit, ...], SimulationResult]) -> tuple[int, int]:
        return result[1].total_reward_units, -result[1].finish_seconds

    for order in orders:
        candidate = insert_additional_contracts(
            problem,
            graph,
            restart_visits,
            restart_simulation,
            candidate_order=order,
        )
        if quality(candidate) > quality(best):
            best = candidate
    seed_visits = best[0]
    selected = {v.contract_id for v in seed_visits if isinstance(v, PlannedAction)}
    items = {i.contract_id: i for i in problem.contracts}
    for removed in sorted(selected):
        reduced = _remove_contract_visits(
            seed_visits,
            frozenset({removed}),
            items,
            problem.constraints.required_system_ids,
        )
        sim = simulate_and_verify(
            problem,
            graph,
            reduced,
            tuple(sorted(selected - {removed})),
        )
        if not sim.report.valid:
            continue
        for order in orders[:2]:
            candidate = insert_additional_contracts(
                problem,
                graph,
                reduced,
                sim,
                candidate_order=order,
            )
            if quality(candidate) > quality(best):
                best = candidate
    return best


def build_greedy_route_hint(problem: RouteProblem) -> tuple[PlannedVisit, ...]:
    """Build a deterministic sequential route with a feasible required-system tail.

    Hints never constrain the model. This conservative constructor selects only jobs it can visit
    pickup-then-delivery without interleaving while still reserving a concrete path through every
    remaining required system and the terminal. CP-SAT remains free to improve it or ignore it.
    """

    if problem.active_shipments:
        return ()
    constraints = problem.constraints
    current_system_id = constraints.start_system_id
    elapsed_seconds = 0
    locked_collateral_units = 0
    visits: list[PlannedVisit] = []
    contracts_by_score = sorted(
        problem.scores,
        key=lambda score: (
            score.reward_per_hour_isk,
            score.contract.reward_units,
            -score.contract.contract_id,
        ),
        reverse=True,
    )
    service_time_seconds = constraints.travel.service_seconds
    seconds_per_jump = constraints.travel.seconds_per_jump
    required_system_ids = set(constraints.required_system_ids)
    required_system_ids.discard(constraints.start_system_id)
    visited_required_system_ids: set[int] = set()

    def finish_required_route_seconds(
        starting_system_id: int,
        starting_elapsed_seconds: int,
        already_visited_system_ids: set[int],
    ) -> int | None:
        """Return one deterministic feasible waypoint/terminal tail completion time."""

        current_system_id = starting_system_id
        completion_seconds = starting_elapsed_seconds
        remaining_required_system_ids = required_system_ids - already_visited_system_ids
        terminal_system_id = constraints.terminal_system_id
        if terminal_system_id is not None:
            remaining_required_system_ids.discard(terminal_system_id)
        while remaining_required_system_ids:
            reachable_required_systems = [
                (jump_count, candidate_system_id)
                for candidate_system_id in remaining_required_system_ids
                if (jump_count := problem.jump_matrix.get((current_system_id, candidate_system_id)))
                is not None
            ]
            if not reachable_required_systems:
                return None
            jump_count, destination_system_id = min(reachable_required_systems)
            completion_seconds += jump_count * seconds_per_jump
            current_system_id = destination_system_id
            remaining_required_system_ids.remove(destination_system_id)
        if terminal_system_id is not None:
            jump_count = problem.jump_matrix.get((current_system_id, terminal_system_id))
            if jump_count is None:
                return None
            completion_seconds += jump_count * seconds_per_jump
        return completion_seconds

    for score in contracts_by_score:
        contract = score.contract
        jumps_to_pickup = problem.jump_matrix.get((current_system_id, contract.origin_system_id))
        delivery_jumps = problem.jump_matrix.get(
            (contract.origin_system_id, contract.destination_system_id)
        )
        if jumps_to_pickup is None or delivery_jumps is None:
            continue
        pickup_arrival_seconds = elapsed_seconds + jumps_to_pickup * seconds_per_jump
        delivery_arrival_seconds = (
            pickup_arrival_seconds + service_time_seconds + delivery_jumps * seconds_per_jump
        )
        delivery_completion_seconds = delivery_arrival_seconds + service_time_seconds
        if delivery_completion_seconds > constraints.horizon_seconds:
            continue
        newly_visited_required_system_ids = visited_required_system_ids | (
            {contract.origin_system_id, contract.destination_system_id} & required_system_ids
        )
        route_finish_seconds = finish_required_route_seconds(
            contract.destination_system_id,
            delivery_completion_seconds,
            newly_visited_required_system_ids,
        )
        if route_finish_seconds is None or route_finish_seconds > constraints.horizon_seconds:
            continue
        if constraints.collateral_mode is CollateralMode.LOCKED:
            if (
                locked_collateral_units + contract.collateral_units
                > constraints.collateral_budget_units
            ):
                continue
            if delivery_completion_seconds > contract.days_to_complete * 86_400:
                continue
        else:
            if pickup_arrival_seconds > contract.last_pickup_second(constraints.snapshot_time):
                continue
            if (
                delivery_completion_seconds
                > pickup_arrival_seconds + contract.days_to_complete * 86_400
            ):
                continue
        visits.extend(
            (
                PlannedAction(ActionKind.PICKUP, contract.contract_id),
                PlannedAction(ActionKind.DELIVERY, contract.contract_id),
            )
        )
        if constraints.collateral_mode is CollateralMode.LOCKED:
            locked_collateral_units += contract.collateral_units
        elapsed_seconds = delivery_completion_seconds
        current_system_id = contract.destination_system_id
        visited_required_system_ids = newly_visited_required_system_ids
    remaining = required_system_ids - visited_required_system_ids
    if constraints.terminal_system_id is not None:
        remaining.discard(constraints.terminal_system_id)
    while remaining:
        choices = [
            (jumps, system)
            for system in remaining
            if (jumps := problem.jump_matrix.get((current_system_id, system))) is not None
        ]
        if not choices:
            return ()
        _, current_system_id = min(choices)
        visits.append(PlannedWaypoint(current_system_id))
        remaining.remove(current_system_id)
    return tuple(visits)


def construct_incumbent(problem: RouteProblem, graph: UniverseGraph) -> VerifiedRoute | None:
    optional_ids = {contract.contract_id for contract in problem.contracts}
    visits = build_greedy_route_hint(problem)
    if problem.active_shipments:
        actions: list[PlannedVisit] = []
        for shipment in sorted(
            problem.active_shipments,
            key=lambda shipment: (
                not shipment.picked,
                shipment.deadline,
                shipment.contract.contract_id,
            ),
        ):
            if not shipment.picked:
                actions.append(PlannedAction(ActionKind.PICKUP, shipment.contract.contract_id))
            actions.append(PlannedAction(ActionKind.DELIVERY, shipment.contract.contract_id))
        actions.extend(
            PlannedWaypoint(system) for system in sorted(problem.constraints.required_system_ids)
        )
        visits = tuple(actions)
    ids = tuple(
        sorted(
            {
                visit.contract_id
                for visit in visits
                if isinstance(visit, PlannedAction) and visit.contract_id in optional_ids
            }
        )
    )
    simulation = simulate_and_verify(problem, graph, visits, ids)
    mandatory_only = replace(problem, contracts=(), scores=())
    visits, simulation = improve_incumbent(
        problem, graph, visits, simulation, restart_visits=build_greedy_route_hint(mandatory_only)
    )
    if not simulation.report.valid:
        return None
    ids = tuple(
        sorted(
            {
                visit.contract_id
                for visit in visits
                if isinstance(visit, PlannedAction) and visit.contract_id in optional_ids
            }
        )
    )
    return VerifiedRoute(ids, simulation)


def diversify_incumbent(
    problem: RouteProblem,
    graph: UniverseGraph,
    incumbent: VerifiedRoute,
    *,
    reward_ceiling: int | None,
    time_budget_seconds: float,
) -> VerifiedRoute:
    """Explore different haul lanes and two-job replacements after bounded exact search.

    Only an unproven result with a master bound pays this cost. Every candidate is independently
    replayed; the neighborhood improves the final route, never eligibility or the rigorous ceiling.
    The soft budget is checked between insertion passes, not inside the route verifier.
    """
    if problem.active_shipments or time_budget_seconds <= 0:
        return incumbent
    deadline = time.perf_counter() + time_budget_seconds
    orders = _insertion_orders(problem)[:2]
    best = incumbent

    def finished() -> bool:
        return (
            best.simulation.total_reward_units == reward_ceiling or time.perf_counter() >= deadline
        )

    def try_insertions(visits: tuple[PlannedVisit, ...], simulation: SimulationResult) -> None:
        nonlocal best
        if not simulation.report.valid:
            return
        for order in orders:
            if finished():
                return
            _, candidate = insert_additional_contracts(
                problem, graph, visits, simulation, candidate_order=order
            )
            if (candidate.total_reward_units, -candidate.finish_seconds) > best.quality:
                ids = tuple(
                    sorted(
                        {
                            visit.contract_id
                            for visit in candidate.visits
                            if isinstance(visit, PlannedAction)
                        }
                    )
                )
                best = VerifiedRoute(ids, candidate)

    tried_lanes: set[tuple[int, int]] = set()
    for score in orders[1]:
        if finished():
            return best
        contract = score.contract
        lane = contract.origin_system_id, contract.destination_system_id
        if lane in tried_lanes:
            continue
        tried_lanes.add(lane)
        seed_problem = replace(problem, contracts=(contract,), scores=(score,))
        visits = build_greedy_route_hint(seed_problem)
        ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
        simulation = simulate_and_verify(problem, graph, visits, ids)
        try_insertions(visits, simulation)

    seed = best
    contracts = {contract.contract_id: contract for contract in problem.contracts}
    for removed in combinations(seed.selected_contract_ids, 2):
        if finished():
            break
        visits = _remove_contract_visits(
            seed.simulation.visits,
            frozenset(removed),
            contracts,
            problem.constraints.required_system_ids,
        )
        ids = tuple(cid for cid in seed.selected_contract_ids if cid not in removed)
        simulation = simulate_and_verify(problem, graph, visits, ids)
        try_insertions(visits, simulation)
    return best
