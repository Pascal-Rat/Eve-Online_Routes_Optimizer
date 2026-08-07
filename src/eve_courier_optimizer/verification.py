"""Independent route simulation used after optimization and by tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .domain import (
    ActionKind,
    CollateralMode,
    RouteProblem,
    RouteStep,
    TravelLeg,
    TravelLegKind,
    ValidationReport,
)
from .sde import UniverseGraph


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action: ActionKind
    contract_id: int


@dataclass(frozen=True, slots=True)
class PlannedWaypoint:
    system_id: int


type PlannedVisit = PlannedAction | PlannedWaypoint


@dataclass(frozen=True, slots=True)
class SimulationResult:
    steps: tuple[RouteStep, ...]
    travel_legs: tuple[TravelLeg, ...]
    total_reward_units: int
    finish_seconds: int
    report: ValidationReport


def simulate_and_verify(
    problem: RouteProblem,
    graph: UniverseGraph,
    visits: tuple[PlannedVisit, ...],
    selected_contract_ids: tuple[int, ...],
) -> SimulationResult:
    """Simulate a solver route without using solver variables.

    This is intentionally independent of CP-SAT load/time variables. A model-extraction bug must
    therefore fail verification before a proof is reported to the user.
    """

    constraints = problem.constraints
    optional = {item.contract.contract_id: item for item in problem.contracts}
    active = {
        item.contract.contract.contract_id: item
        for item in problem.active_shipments
    }
    selected = set(selected_contract_ids)
    violations: list[str] = []
    if not selected.issubset(optional):
        violations.append("selected contract set contains an unknown optional contract")

    cargo = sum(
        item.contract.contract.volume_units for item in active.values() if item.picked
    )
    if constraints.collateral_mode is CollateralMode.LOCKED:
        collateral = sum(item.contract.contract.collateral_units for item in active.values())
        collateral += sum(
            optional[item].contract.collateral_units for item in selected if item in optional
        )
    else:
        collateral = sum(item.contract.contract.collateral_units for item in active.values())
    reward = 0
    current_system = constraints.start_system_id
    current_time = 0
    picked: set[int] = {
        contract_id for contract_id, shipment in active.items() if shipment.picked
    }
    delivered: set[int] = set()
    pickup_times: dict[int, int] = {}
    steps: list[RouteStep] = []
    travel_legs: list[TravelLeg] = []
    visited_systems = {current_system}
    action_sequence = 0

    if cargo > constraints.cargo_capacity_units:
        violations.append("initial cargo exceeds capacity")
    if collateral > constraints.collateral_budget_units:
        violations.append("initial collateral exceeds budget")
    if (
        constraints.max_simultaneous_contracts is not None
        and len(picked - delivered) > constraints.max_simultaneous_contracts
    ):
        violations.append("initial simultaneous-contract limit exceeded")

    for visit_sequence, planned in enumerate(visits, start=1):
        if isinstance(planned, PlannedWaypoint):
            jump_path = graph.shortest_path(
                current_system,
                planned.system_id,
                constraints.security,
            )
            if jump_path is None:
                violations.append(f"no permitted route to waypoint {visit_sequence}")
                break
            arrival = current_time + (len(jump_path) - 1) * constraints.travel.seconds_per_jump
            if arrival > constraints.horizon_seconds:
                violations.append(f"planning horizon exceeded at waypoint {visit_sequence}")
            travel_legs.append(
                TravelLeg(
                    sequence=len(travel_legs) + 1,
                    kind=TravelLegKind.WAYPOINT,
                    from_system_id=current_system,
                    to_system_id=planned.system_id,
                    arrival_seconds=arrival,
                    completion_seconds=arrival,
                    jump_path=jump_path,
                )
            )
            visited_systems.update(jump_path)
            current_time = arrival
            current_system = planned.system_id
            continue

        action_sequence += 1
        optional_contract = optional.get(planned.contract_id)
        active_shipment = active.get(planned.contract_id)
        if optional_contract is None and active_shipment is None:
            violations.append(f"action references unknown contract {planned.contract_id}")
            continue
        if active_shipment is not None:
            contract = active_shipment.contract
            if planned.action is ActionKind.PICKUP and active_shipment.picked:
                violations.append(
                    f"active shipment {planned.contract_id} cannot be picked up again"
                )
                continue
        else:
            assert optional_contract is not None
            contract = optional_contract
            if planned.contract_id not in selected:
                violations.append(f"action references unselected contract {planned.contract_id}")

        target_system = (
            contract.origin_system_id
            if planned.action is ActionKind.PICKUP
            else contract.destination_system_id
        )
        target_location = (
            contract.contract.origin_location_id
            if planned.action is ActionKind.PICKUP
            else contract.contract.destination_location_id
        )
        jump_path = graph.shortest_path(current_system, target_system, constraints.security)
        if jump_path is None:
            violations.append(f"no permitted route to action {action_sequence}")
            break
        jumps = len(jump_path) - 1
        arrival = current_time + jumps * constraints.travel.seconds_per_jump
        completion = arrival + constraints.travel.service_seconds

        if planned.action is ActionKind.PICKUP:
            if planned.contract_id in picked:
                violations.append(f"contract {planned.contract_id} picked up more than once")
            if planned.contract_id in delivered:
                violations.append(f"contract {planned.contract_id} picked up after delivery")
            picked.add(planned.contract_id)
            pickup_times[planned.contract_id] = arrival
            cargo += contract.contract.volume_units
            if (
                constraints.collateral_mode is CollateralMode.ROLLING
                and active_shipment is None
            ):
                collateral += contract.contract.collateral_units
                acceptance_time = constraints.snapshot_time + timedelta(seconds=arrival)
                if acceptance_time >= contract.contract.date_expired:
                    violations.append(
                        f"contract {planned.contract_id} accepted after listing expiry"
                    )
        else:
            if planned.contract_id not in picked:
                violations.append(f"contract {planned.contract_id} delivered before pickup")
            if planned.contract_id in delivered:
                violations.append(f"contract {planned.contract_id} delivered more than once")
            delivered.add(planned.contract_id)
            cargo -= contract.contract.volume_units
            collateral -= contract.contract.collateral_units
            reward += contract.contract.reward_units
            if active_shipment is not None:
                deadline_seconds = int(
                    (active_shipment.deadline - constraints.snapshot_time).total_seconds()
                )
                if completion > deadline_seconds:
                    violations.append(f"active shipment {planned.contract_id} misses its deadline")
            elif constraints.collateral_mode is CollateralMode.LOCKED:
                deadline_seconds = contract.contract.days_to_complete * 86_400
                if completion > deadline_seconds:
                    violations.append(
                        f"contract {planned.contract_id} misses its delivery deadline"
                    )
            else:
                pickup_seconds = pickup_times.get(planned.contract_id)
                if pickup_seconds is not None:
                    deadline_seconds = (
                        pickup_seconds + contract.contract.days_to_complete * 86_400
                    )
                    if completion > deadline_seconds:
                        violations.append(
                            f"contract {planned.contract_id} misses its delivery deadline"
                        )

        if cargo < 0 or cargo > constraints.cargo_capacity_units:
            violations.append(f"cargo capacity violated after action {action_sequence}")
        if collateral < 0 or collateral > constraints.collateral_budget_units:
            violations.append(f"collateral budget violated after action {action_sequence}")
        if (
            constraints.max_simultaneous_contracts is not None
            and len(picked - delivered) > constraints.max_simultaneous_contracts
        ):
            violations.append(
                f"simultaneous-contract limit violated after action {action_sequence}"
            )
        if completion > constraints.horizon_seconds:
            violations.append(f"planning horizon exceeded at action {action_sequence}")

        steps.append(
            RouteStep(
                sequence=action_sequence,
                action=planned.action,
                contract_id=planned.contract_id,
                system_id=target_system,
                location_id=target_location,
                arrival_seconds=arrival,
                completion_seconds=completion,
                cargo_after_units=cargo,
                collateral_after_units=collateral,
                cumulative_reward_units=reward,
                jump_path=jump_path,
            )
        )
        travel_legs.append(
            TravelLeg(
                sequence=len(travel_legs) + 1,
                kind=(
                    TravelLegKind.PICKUP
                    if planned.action is ActionKind.PICKUP
                    else TravelLegKind.DELIVERY
                ),
                from_system_id=current_system,
                to_system_id=target_system,
                arrival_seconds=arrival,
                completion_seconds=completion,
                jump_path=jump_path,
                contract_id=planned.contract_id,
            )
        )
        visited_systems.update(jump_path)
        current_time = completion
        current_system = target_system

    terminal_system_id = constraints.terminal_system_id
    if terminal_system_id is not None:
        jump_path = graph.shortest_path(current_system, terminal_system_id, constraints.security)
        if jump_path is None:
            violations.append("no permitted route to required finish system")
        else:
            arrival = current_time + (len(jump_path) - 1) * constraints.travel.seconds_per_jump
            if arrival > constraints.horizon_seconds:
                violations.append("planning horizon exceeded before required finish system")
            travel_legs.append(
                TravelLeg(
                    sequence=len(travel_legs) + 1,
                    kind=TravelLegKind.FINISH,
                    from_system_id=current_system,
                    to_system_id=terminal_system_id,
                    arrival_seconds=arrival,
                    completion_seconds=arrival,
                    jump_path=jump_path,
                )
            )
            visited_systems.update(jump_path)
            current_time = arrival
            current_system = terminal_system_id

    missing_required = constraints.required_system_ids - visited_systems
    if missing_required:
        violations.append(
            "route does not visit every required system: "
            + ", ".join(str(system_id) for system_id in sorted(missing_required))
        )

    if not selected.issubset(picked):
        violations.append("not every selected contract was picked up exactly once")
    if not selected.issubset(delivered):
        violations.append("not every selected contract was delivered")
    if set(active) - delivered:
        violations.append("not every active shipment was delivered")
    if cargo != 0:
        violations.append("route finishes with non-empty courier cargo")
    if collateral != 0:
        violations.append("route finishes with collateral still locked")

    return SimulationResult(
        steps=tuple(steps),
        travel_legs=tuple(travel_legs),
        total_reward_units=reward,
        finish_seconds=current_time,
        report=ValidationReport(valid=not violations, violations=tuple(violations)),
    )
