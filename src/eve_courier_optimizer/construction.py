"""Constructive route improvements used only as independently verified search incumbents."""

from __future__ import annotations

from .domain import ActionKind, CollateralMode
from .planning import PreparedProblem
from .sde import UniverseGraph
from .verification import PlannedAction, PlannedVisit, SimulationResult, simulate_and_verify


def insert_additional_contracts(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    visits: tuple[PlannedVisit, ...],
    simulation: SimulationResult,
) -> tuple[tuple[PlannedVisit, ...], SimulationResult]:
    """Try each unused contract in every precedence-respecting pair of insertion positions.

    Existing actions retain their order. Cheap distance and interval-load checks rank insertions;
    the independent simulator decides acceptance, including deadlines and route requirements.
    This adds shared-haul opportunities to a sequential seed without committing the exact search
    to its choices. Active commitments currently rely on the exact constructor instead.
    """
    if not simulation.report.valid or prepared.problem.active_shipments:
        return visits, simulation
    c = prepared.problem.constraints
    contracts = {item.contract.contract_id: item for item in prepared.problem.contracts}
    selected = {visit.contract_id for visit in visits if isinstance(visit, PlannedAction)}
    candidates = sorted(
        prepared.scores,
        key=lambda score: (
            score.reward_per_hour_isk,
            score.contract.contract.reward_units,
            -score.contract.contract.contract_id,
        ),
        reverse=True,
    )

    def distance(source: int, destination: int | None) -> int:
        if destination is None:
            return 0
        jumps = prepared.jump_matrix.get((source, destination))
        # Unreachable insertions cannot beat the horizon; avoid special arithmetic at each slot.
        return c.horizon_seconds + 1 if jumps is None else jumps * c.travel.seconds_per_jump

    for score in candidates:
        item = score.contract
        contract = item.contract
        if contract.contract_id in selected:
            continue
        if simulation.finish_seconds + 2 * c.travel.service_seconds > c.horizon_seconds:
            break
        if c.collateral_mode is CollateralMode.LOCKED and (
            sum(contracts[i].contract.collateral_units for i in selected)
            + contract.collateral_units
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
                load += sign * existing.contract.volume_units
                count += sign
                locked += sign * existing.contract.collateral_units
            else:
                systems.append(visit.system_id)
            cargo.append(load)
            parcels.append(count)
            collateral.append(locked)
        endpoints: list[int | None] = [*systems[1:], c.terminal_system_id]
        origin, destination = item.origin_system_id, item.destination_system_id
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
                prepared.problem, graph, trial, tuple(sorted(selected | {contract.contract_id}))
            )
            if trial_simulation.report.valid:
                visits, simulation = trial, trial_simulation
                selected.add(contract.contract_id)
                break
    return visits, simulation
