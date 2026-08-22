"""Verify an optimized route by replaying it without using solver variables.

The optimizer and this module intentionally reach the same answer in different ways. CP-SAT uses
integer variables and constraints to construct a route; the verifier simply walks that route in
order and updates ordinary Python state. A mistake in model construction or route extraction is
therefore much less likely to produce a false optimality claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .domain import (
    ActionKind,
    ActiveShipment,
    CollateralMode,
    PlanningConstraints,
    RoutableContract,
    RouteProblem,
    RouteStep,
    TravelLeg,
    TravelLegKind,
    ValidationReport,
)
from .sde import UniverseGraph


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """A pickup or delivery chosen by the optimizer."""

    action: ActionKind
    contract_id: int


@dataclass(frozen=True, slots=True)
class PlannedWaypoint:
    """A system that the route must visit without servicing a contract there."""

    system_id: int


type PlannedVisit = PlannedAction | PlannedWaypoint


@dataclass(frozen=True, slots=True)
class SimulationResult:
    steps: tuple[RouteStep, ...]
    travel_legs: tuple[TravelLeg, ...]
    total_reward_units: int
    finish_seconds: int
    report: ValidationReport


@dataclass(slots=True)
class _SimulationState:
    """Values that change while the verifier replays a proposed route."""

    current_system_id: int
    elapsed_seconds: int
    cargo_load_units: int
    locked_collateral_units: int
    earned_reward_units: int
    picked_contract_ids: set[int]
    delivered_contract_ids: set[int]
    pickup_time_by_contract_id: dict[int, int]
    visited_system_ids: set[int]
    steps: list[RouteStep]
    travel_legs: list[TravelLeg]
    violations: list[str]
    action_sequence: int = 0


def _initial_simulation_state(
    problem: RouteProblem,
    optional_contract_by_id: dict[int, RoutableContract],
    active_shipment_by_id: dict[int, ActiveShipment],
    selected_contract_ids: set[int],
) -> _SimulationState:
    """Create the route state at the planning snapshot.

    Active shipments already hold collateral. Shipments marked as picked also start in cargo.
    In locked-collateral mode, every selected optional contract reserves collateral immediately;
    rolling mode reserves it only when the route reaches the pickup.
    """

    constraints = problem.constraints
    cargo_load_units = sum(
        shipment.contract.contract.volume_units
        for shipment in active_shipment_by_id.values()
        if shipment.picked
    )
    locked_collateral_units = sum(
        shipment.contract.contract.collateral_units for shipment in active_shipment_by_id.values()
    )
    if constraints.collateral_mode is CollateralMode.LOCKED:
        locked_collateral_units += sum(
            optional_contract_by_id[contract_id].contract.collateral_units
            for contract_id in selected_contract_ids
            if contract_id in optional_contract_by_id
        )

    return _SimulationState(
        current_system_id=constraints.start_system_id,
        elapsed_seconds=0,
        cargo_load_units=cargo_load_units,
        locked_collateral_units=locked_collateral_units,
        earned_reward_units=0,
        picked_contract_ids={
            contract_id
            for contract_id, shipment in active_shipment_by_id.items()
            if shipment.picked
        },
        delivered_contract_ids=set(),
        pickup_time_by_contract_id={},
        visited_system_ids={constraints.start_system_id},
        steps=[],
        travel_legs=[],
        violations=[],
    )


def _record_initial_limit_violations(
    state: _SimulationState,
    constraints: PlanningConstraints,
) -> None:
    if state.cargo_load_units > constraints.cargo_capacity_units:
        state.violations.append("initial cargo exceeds capacity")
    if state.locked_collateral_units > constraints.collateral_budget_units:
        state.violations.append("initial collateral exceeds budget")
    if (
        constraints.max_simultaneous_contracts is not None
        and len(state.picked_contract_ids - state.delivered_contract_ids)
        > constraints.max_simultaneous_contracts
    ):
        state.violations.append("initial simultaneous-contract limit exceeded")


def _visit_waypoint(
    waypoint: PlannedWaypoint,
    visit_sequence: int,
    state: _SimulationState,
    constraints: PlanningConstraints,
    graph: UniverseGraph,
) -> bool:
    """Travel to a required system; return ``False`` when no permitted path exists."""

    jump_path = graph.shortest_path(
        state.current_system_id,
        waypoint.system_id,
        constraints.security,
    )
    if jump_path is None:
        state.violations.append(f"no permitted route to waypoint {visit_sequence}")
        return False

    arrival_seconds = (
        state.elapsed_seconds + (len(jump_path) - 1) * constraints.travel.seconds_per_jump
    )
    if arrival_seconds > constraints.horizon_seconds:
        state.violations.append(f"planning horizon exceeded at waypoint {visit_sequence}")
    state.travel_legs.append(
        TravelLeg(
            sequence=len(state.travel_legs) + 1,
            kind=TravelLegKind.WAYPOINT,
            from_system_id=state.current_system_id,
            to_system_id=waypoint.system_id,
            arrival_seconds=arrival_seconds,
            completion_seconds=arrival_seconds,
            jump_path=jump_path,
        )
    )
    state.visited_system_ids.update(jump_path)
    state.elapsed_seconds = arrival_seconds
    state.current_system_id = waypoint.system_id
    return True


def _resolve_planned_contract(
    planned_action: PlannedAction,
    state: _SimulationState,
    optional_contract_by_id: dict[int, RoutableContract],
    active_shipment_by_id: dict[int, ActiveShipment],
    selected_contract_ids: set[int],
) -> tuple[RoutableContract | None, ActiveShipment | None]:
    """Find the action's contract and record selection/state errors."""

    contract_id = planned_action.contract_id
    optional_contract = optional_contract_by_id.get(contract_id)
    active_shipment = active_shipment_by_id.get(contract_id)
    if optional_contract is None and active_shipment is None:
        state.violations.append(f"action references unknown contract {contract_id}")
        return None, None

    if active_shipment is not None:
        if planned_action.action is ActionKind.PICKUP and active_shipment.picked:
            state.violations.append(f"active shipment {contract_id} cannot be picked up again")
            return None, active_shipment
        return active_shipment.contract, active_shipment

    assert optional_contract is not None
    if contract_id not in selected_contract_ids:
        state.violations.append(f"action references unselected contract {contract_id}")
    return optional_contract, None


def _apply_pickup(
    planned_action: PlannedAction,
    contract: RoutableContract,
    active_shipment: ActiveShipment | None,
    arrival_seconds: int,
    state: _SimulationState,
    constraints: PlanningConstraints,
) -> None:
    contract_id = planned_action.contract_id
    if contract_id in state.picked_contract_ids:
        state.violations.append(f"contract {contract_id} picked up more than once")
    if contract_id in state.delivered_contract_ids:
        state.violations.append(f"contract {contract_id} picked up after delivery")

    state.picked_contract_ids.add(contract_id)
    state.pickup_time_by_contract_id[contract_id] = arrival_seconds
    state.cargo_load_units += contract.contract.volume_units

    if constraints.collateral_mode is CollateralMode.ROLLING and active_shipment is None:
        state.locked_collateral_units += contract.contract.collateral_units
        acceptance_time = constraints.snapshot_time + timedelta(seconds=arrival_seconds)
        if acceptance_time >= contract.contract.date_expired:
            state.violations.append(f"contract {contract_id} accepted after listing expiry")


def _apply_delivery(
    planned_action: PlannedAction,
    contract: RoutableContract,
    active_shipment: ActiveShipment | None,
    completion_seconds: int,
    state: _SimulationState,
    constraints: PlanningConstraints,
) -> None:
    contract_id = planned_action.contract_id
    if contract_id not in state.picked_contract_ids:
        state.violations.append(f"contract {contract_id} delivered before pickup")
    if contract_id in state.delivered_contract_ids:
        state.violations.append(f"contract {contract_id} delivered more than once")

    state.delivered_contract_ids.add(contract_id)
    state.cargo_load_units -= contract.contract.volume_units
    state.locked_collateral_units -= contract.contract.collateral_units
    state.earned_reward_units += contract.contract.reward_units

    if active_shipment is not None:
        deadline_seconds = int(
            (active_shipment.deadline - constraints.snapshot_time).total_seconds()
        )
    elif constraints.collateral_mode is CollateralMode.LOCKED:
        deadline_seconds = contract.contract.days_to_complete * 86_400
    else:
        pickup_seconds = state.pickup_time_by_contract_id.get(contract_id)
        if pickup_seconds is None:
            # The missing pickup has already been recorded above. Without its time there is no
            # meaningful rolling deadline to check.
            return
        deadline_seconds = pickup_seconds + contract.contract.days_to_complete * 86_400

    if completion_seconds > deadline_seconds:
        contract_kind = "active shipment" if active_shipment is not None else "contract"
        state.violations.append(f"{contract_kind} {contract_id} misses its deadline")


def _record_action_limit_violations(
    state: _SimulationState,
    constraints: PlanningConstraints,
    completion_seconds: int,
) -> None:
    action_sequence = state.action_sequence
    if not 0 <= state.cargo_load_units <= constraints.cargo_capacity_units:
        state.violations.append(f"cargo capacity violated after action {action_sequence}")
    if not 0 <= state.locked_collateral_units <= constraints.collateral_budget_units:
        state.violations.append(f"collateral budget violated after action {action_sequence}")
    if (
        constraints.max_simultaneous_contracts is not None
        and len(state.picked_contract_ids - state.delivered_contract_ids)
        > constraints.max_simultaneous_contracts
    ):
        state.violations.append(
            f"simultaneous-contract limit violated after action {action_sequence}"
        )
    if completion_seconds > constraints.horizon_seconds:
        state.violations.append(f"planning horizon exceeded at action {action_sequence}")


def _execute_contract_action(
    planned_action: PlannedAction,
    state: _SimulationState,
    constraints: PlanningConstraints,
    graph: UniverseGraph,
    optional_contract_by_id: dict[int, RoutableContract],
    active_shipment_by_id: dict[int, ActiveShipment],
    selected_contract_ids: set[int],
) -> bool:
    """Replay one pickup or delivery; return ``False`` when travel is impossible."""

    state.action_sequence += 1
    contract, active_shipment = _resolve_planned_contract(
        planned_action,
        state,
        optional_contract_by_id,
        active_shipment_by_id,
        selected_contract_ids,
    )
    if contract is None:
        return True

    is_pickup = planned_action.action is ActionKind.PICKUP
    target_system_id = contract.origin_system_id if is_pickup else contract.destination_system_id
    target_location_id = (
        contract.contract.origin_location_id
        if is_pickup
        else contract.contract.destination_location_id
    )
    jump_path = graph.shortest_path(
        state.current_system_id,
        target_system_id,
        constraints.security,
    )
    if jump_path is None:
        state.violations.append(f"no permitted route to action {state.action_sequence}")
        return False

    arrival_seconds = (
        state.elapsed_seconds + (len(jump_path) - 1) * constraints.travel.seconds_per_jump
    )
    completion_seconds = arrival_seconds + constraints.travel.service_seconds

    if is_pickup:
        _apply_pickup(
            planned_action,
            contract,
            active_shipment,
            arrival_seconds,
            state,
            constraints,
        )
    else:
        _apply_delivery(
            planned_action,
            contract,
            active_shipment,
            completion_seconds,
            state,
            constraints,
        )

    _record_action_limit_violations(state, constraints, completion_seconds)
    state.steps.append(
        RouteStep(
            sequence=state.action_sequence,
            action=planned_action.action,
            contract_id=planned_action.contract_id,
            system_id=target_system_id,
            location_id=target_location_id,
            arrival_seconds=arrival_seconds,
            completion_seconds=completion_seconds,
            cargo_after_units=state.cargo_load_units,
            collateral_after_units=state.locked_collateral_units,
            cumulative_reward_units=state.earned_reward_units,
            jump_path=jump_path,
        )
    )
    state.travel_legs.append(
        TravelLeg(
            sequence=len(state.travel_legs) + 1,
            kind=(TravelLegKind.PICKUP if is_pickup else TravelLegKind.DELIVERY),
            from_system_id=state.current_system_id,
            to_system_id=target_system_id,
            arrival_seconds=arrival_seconds,
            completion_seconds=completion_seconds,
            jump_path=jump_path,
            contract_id=planned_action.contract_id,
        )
    )
    state.visited_system_ids.update(jump_path)
    state.elapsed_seconds = completion_seconds
    state.current_system_id = target_system_id
    return True


def _travel_to_required_finish(
    state: _SimulationState,
    constraints: PlanningConstraints,
    graph: UniverseGraph,
) -> None:
    terminal_system_id = constraints.terminal_system_id
    if terminal_system_id is None:
        return

    jump_path = graph.shortest_path(
        state.current_system_id,
        terminal_system_id,
        constraints.security,
    )
    if jump_path is None:
        state.violations.append("no permitted route to required finish system")
        return

    arrival_seconds = (
        state.elapsed_seconds + (len(jump_path) - 1) * constraints.travel.seconds_per_jump
    )
    if arrival_seconds > constraints.horizon_seconds:
        state.violations.append("planning horizon exceeded before required finish system")
    state.travel_legs.append(
        TravelLeg(
            sequence=len(state.travel_legs) + 1,
            kind=TravelLegKind.FINISH,
            from_system_id=state.current_system_id,
            to_system_id=terminal_system_id,
            arrival_seconds=arrival_seconds,
            completion_seconds=arrival_seconds,
            jump_path=jump_path,
        )
    )
    state.visited_system_ids.update(jump_path)
    state.elapsed_seconds = arrival_seconds
    state.current_system_id = terminal_system_id


def _record_route_completion_violations(
    state: _SimulationState,
    constraints: PlanningConstraints,
    selected_contract_ids: set[int],
    active_contract_ids: set[int],
) -> None:
    missing_required_system_ids = constraints.required_system_ids - state.visited_system_ids
    if missing_required_system_ids:
        state.violations.append(
            "route does not visit every required system: "
            + ", ".join(str(system_id) for system_id in sorted(missing_required_system_ids))
        )
    if not selected_contract_ids.issubset(state.picked_contract_ids):
        state.violations.append("not every selected contract was picked up exactly once")
    if not selected_contract_ids.issubset(state.delivered_contract_ids):
        state.violations.append("not every selected contract was delivered")
    if active_contract_ids - state.delivered_contract_ids:
        state.violations.append("not every active shipment was delivered")
    if state.cargo_load_units != 0:
        state.violations.append("route finishes with non-empty courier cargo")
    if state.locked_collateral_units != 0:
        state.violations.append("route finishes with collateral still locked")


def simulate_and_verify(
    problem: RouteProblem,
    graph: UniverseGraph,
    visits: tuple[PlannedVisit, ...],
    selected_contract_ids: tuple[int, ...],
) -> SimulationResult:
    """Replay a proposed route and independently check every planning constraint."""

    constraints = problem.constraints
    optional_contract_by_id = {
        contract.contract.contract_id: contract for contract in problem.contracts
    }
    active_shipment_by_id = {
        shipment.contract.contract.contract_id: shipment for shipment in problem.active_shipments
    }
    selected_contract_id_set = set(selected_contract_ids)
    state = _initial_simulation_state(
        problem,
        optional_contract_by_id,
        active_shipment_by_id,
        selected_contract_id_set,
    )

    if not selected_contract_id_set.issubset(optional_contract_by_id):
        state.violations.append("selected contract set contains an unknown optional contract")
    _record_initial_limit_violations(state, constraints)

    # Replay the optimizer's route exactly as given. Stop only when a requested leg cannot be
    # traveled; semantic errors are accumulated so one verification report can explain several
    # problems at once.
    for visit_sequence, planned_visit in enumerate(visits, start=1):
        if isinstance(planned_visit, PlannedWaypoint):
            can_continue = _visit_waypoint(
                planned_visit,
                visit_sequence,
                state,
                constraints,
                graph,
            )
        else:
            can_continue = _execute_contract_action(
                planned_visit,
                state,
                constraints,
                graph,
                optional_contract_by_id,
                active_shipment_by_id,
                selected_contract_id_set,
            )
        if not can_continue:
            break

    _travel_to_required_finish(state, constraints, graph)
    _record_route_completion_violations(
        state,
        constraints,
        selected_contract_id_set,
        set(active_shipment_by_id),
    )

    return SimulationResult(
        steps=tuple(state.steps),
        travel_legs=tuple(state.travel_legs),
        total_reward_units=state.earned_reward_units,
        finish_seconds=state.elapsed_seconds,
        report=ValidationReport(
            valid=not state.violations,
            violations=tuple(state.violations),
        ),
    )
