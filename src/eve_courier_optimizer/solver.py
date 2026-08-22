"""Build and solve the exact pickup-and-delivery route model.

The OR-Tools model follows the same concepts as the courier problem:

* each pickup, delivery, required waypoint, start, and finish is a route event;
* a Boolean variable records whether each optional contract is accepted;
* a Boolean variable records whether the route travels directly between two events;
* integer variables track arrival time, visit order, cargo, collateral, and parcel count; and
* the objective maximizes the reward from delivered contracts.

``CpModel.add_circuit`` connects the chosen event-to-event arcs into one route. Optional events
receive a self-loop when their contract is not selected; mandatory events may not be skipped.
The independent simulator in :mod:`eve_courier_optimizer.verification` then checks the extracted
route without trusting any of these model variables.

Dense problems first use a simpler endpoint-system model to establish a reward ceiling. That
ceiling can either close the proof directly or strengthen the complete event-level model.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from importlib.metadata import version as package_version
from itertools import combinations
from typing import Final

from ortools.sat.python import cp_model

from .bounds import (
    SelectionCuts,
    SystemRelaxationBound,
    add_proven_infeasible_selection_cut,
    build_selection_cuts,
    build_system_relaxation_master,
    solve_system_relaxation_master,
)
from .domain import (
    ActionKind,
    CollateralMode,
    OptimalityCertificate,
    ProofStatus,
    SolveResult,
)
from .planning import PreparedProblem
from .proof import canonical_problem_sha256, optimality_claim
from .reference_solver import solve_reference
from .sde import UniverseGraph
from .verification import (
    PlannedAction,
    PlannedVisit,
    PlannedWaypoint,
    SimulationResult,
    simulate_and_verify,
)

_START: Final = "start"
_END: Final = "end"
_START_NODE_ID: Final = 0
_END_NODE_ID: Final = 1
_BOUND_STRENGTHENING_MIN_CONTRACTS: Final = 20


@dataclass(frozen=True, slots=True)
class SolverConfig:
    max_time_seconds: float | None = 300.0
    num_workers: int = 1
    random_seed: int = 0
    log_search_progress: bool = False
    minimize_finish_time_after_proof: bool = True
    secondary_time_seconds: float = 30.0
    independent_reference_limit: int = 10
    relaxation_time_seconds: float = 10.0
    decomposition_time_seconds: float = 20.0
    decomposition_subproblem_time_seconds: float = 2.0
    decomposition_max_iterations: int = 8

    def __post_init__(self) -> None:
        if self.max_time_seconds is not None and self.max_time_seconds <= 0:
            raise ValueError("max_time_seconds must be positive or None")
        if self.num_workers <= 0:
            raise ValueError("num_workers must be positive")
        if self.secondary_time_seconds <= 0:
            raise ValueError("secondary_time_seconds must be positive")
        if self.independent_reference_limit < 0:
            raise ValueError("independent_reference_limit cannot be negative")
        if self.relaxation_time_seconds < 0:
            raise ValueError("relaxation_time_seconds cannot be negative")
        if self.decomposition_time_seconds < 0:
            raise ValueError("decomposition_time_seconds cannot be negative")
        if self.decomposition_subproblem_time_seconds <= 0:
            raise ValueError("decomposition_subproblem_time_seconds must be positive")
        if self.decomposition_max_iterations <= 0:
            raise ValueError("decomposition_max_iterations must be positive")


@dataclass(frozen=True, slots=True)
class _RouteEvent:
    """One location or contract action that may appear in the optimized route."""

    node_id: int
    label: str
    action_kind: ActionKind | None
    contract_id: int | None
    system_id: int | None
    location_id: int | None
    cargo_delta: int = 0
    collateral_delta: int = 0
    parcel_delta: int = 0
    is_optional: bool = False


@dataclass(frozen=True, slots=True)
class _RouteEventCatalog:
    """Route events plus the node IDs needed to add contract constraints."""

    events: tuple[_RouteEvent, ...]
    optional_pickup_node_by_contract_id: dict[int, int]
    optional_delivery_node_by_contract_id: dict[int, int]
    active_pickup_node_by_contract_id: dict[int, int]
    active_delivery_node_by_contract_id: dict[int, int]
    required_waypoint_node_ids: tuple[int, ...]


@dataclass(slots=True)
class _RouteModel:
    """The CP-SAT model plus variables needed to extract its chosen route."""

    model: cp_model.CpModel
    events: tuple[_RouteEvent, ...]
    start_node_id: int
    end_node_id: int
    contract_is_selected: dict[int, cp_model.IntVar]
    arc_is_used: dict[tuple[int, int], cp_model.IntVar]
    total_reward_units: cp_model.IntVar
    finish_time_seconds: cp_model.IntVar


@dataclass(frozen=True, slots=True)
class _SolverRunStats:
    status: cp_model.CpSolverStatus
    status_name: str
    objective_units: int | None
    bound_units: int | None
    wall_time_seconds: float
    branches: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class _ExactOracleResult:
    status: cp_model.CpSolverStatus
    status_name: str
    selected_contract_ids: tuple[int, ...]
    simulation: SimulationResult | None
    infeasible_core_ids: tuple[int, ...]
    wall_time_seconds: float
    branches: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class _DecompositionOutcome:
    relaxation: SystemRelaxationBound | None
    selection_cuts: SelectionCuts
    simulation: SimulationResult | None
    selected_contract_ids: tuple[int, ...]
    proven_infeasible: bool
    status_name: str | None
    iteration_count: int
    learned_infeasibility_cores: tuple[tuple[int, ...], ...]
    subproblem_wall_time_seconds: float
    subproblem_branches: int
    subproblem_conflicts: int


def _new_solver(
    config: SolverConfig,
    *,
    use_secondary_time_limit: bool = False,
) -> cp_model.CpSolver:
    """Create a consistently configured CP-SAT solver for a main or refinement solve."""

    solver = cp_model.CpSolver()
    if use_secondary_time_limit:
        solver.parameters.max_time_in_seconds = config.secondary_time_seconds
    elif config.max_time_seconds is not None:
        solver.parameters.max_time_in_seconds = config.max_time_seconds
    solver.parameters.num_search_workers = config.num_workers
    solver.parameters.random_seed = config.random_seed
    solver.parameters.log_search_progress = config.log_search_progress
    return solver


def _apply_bound_strengthening(
    route_model: _RouteModel,
    relaxation: SystemRelaxationBound | None,
    selection_cuts: SelectionCuts,
    proven_infeasible_contract_sets: tuple[tuple[int, ...], ...] = (),
) -> None:
    """Add redundant constraints that help CP-SAT prove the same answer faster."""

    if relaxation is not None and relaxation.upper_bound_units is not None:
        route_model.model.add(route_model.total_reward_units <= relaxation.upper_bound_units)

    contract_pairs_covered_by_cliques = {
        tuple(sorted(pair))
        for mutually_exclusive_contract_ids in selection_cuts.cliques
        for pair in combinations(mutually_exclusive_contract_ids, 2)
    }
    for mutually_exclusive_contract_ids in selection_cuts.cliques:
        route_model.model.add(
            sum(
                route_model.contract_is_selected[contract_id]
                for contract_id in mutually_exclusive_contract_ids
            )
            <= 1
        )
    for first_contract_id, second_contract_id in selection_cuts.pairs:
        pair = (first_contract_id, second_contract_id)
        if pair not in contract_pairs_covered_by_cliques:
            route_model.model.add(
                route_model.contract_is_selected[first_contract_id]
                + route_model.contract_is_selected[second_contract_id]
                <= 1
            )
    for infeasible_contract_ids in proven_infeasible_contract_sets:
        route_model.model.add(
            sum(
                route_model.contract_is_selected[contract_id]
                for contract_id in infeasible_contract_ids
            )
            <= len(infeasible_contract_ids) - 1
        )


def _last_valid_pickup_second(prepared: PreparedProblem, contract_id: int) -> int:
    """Return the final integral second strictly before a listing expires."""

    contract = next(
        routable.contract
        for routable in prepared.problem.contracts
        if routable.contract.contract_id == contract_id
    )
    return max(
        0,
        math.ceil(
            (contract.date_expired - prepared.problem.constraints.snapshot_time).total_seconds()
        )
        - 1,
    )


def _build_greedy_selection_hint(prepared: PreparedProblem) -> frozenset[int]:
    """Build a deterministic sequential selection hint with a feasible required-route tail.

    Hints never constrain the model. This conservative constructor selects only jobs it can visit
    pickup-then-delivery without interleaving while still reserving a concrete path through every
    remaining required system and the terminal. CP-SAT remains free to improve it or ignore it.
    """

    problem = prepared.problem
    if problem.active_shipments:
        return frozenset()
    constraints = problem.constraints
    current_system_id = constraints.start_system_id
    elapsed_seconds = 0
    locked_collateral_units = 0
    suggested_contract_ids: set[int] = set()
    contracts_by_score = sorted(
        prepared.scores,
        key=lambda score: (
            score.reward_per_hour_isk,
            score.contract.contract.reward_units,
            -score.contract.contract.contract_id,
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
                if (
                    jump_count := prepared.jump_matrix.get((current_system_id, candidate_system_id))
                )
                is not None
            ]
            if not reachable_required_systems:
                return None
            jump_count, destination_system_id = min(reachable_required_systems)
            completion_seconds += jump_count * seconds_per_jump
            current_system_id = destination_system_id
            remaining_required_system_ids.remove(destination_system_id)
        if terminal_system_id is not None:
            jump_count = prepared.jump_matrix.get((current_system_id, terminal_system_id))
            if jump_count is None:
                return None
            completion_seconds += jump_count * seconds_per_jump
        return completion_seconds

    for score in contracts_by_score:
        routable = score.contract
        contract = routable.contract
        jumps_to_pickup = prepared.jump_matrix.get((current_system_id, routable.origin_system_id))
        delivery_jumps = prepared.jump_matrix.get(
            (routable.origin_system_id, routable.destination_system_id)
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
            {routable.origin_system_id, routable.destination_system_id} & required_system_ids
        )
        route_finish_seconds = finish_required_route_seconds(
            routable.destination_system_id,
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
            if pickup_arrival_seconds > _last_valid_pickup_second(
                prepared,
                contract.contract_id,
            ):
                continue
            if (
                delivery_completion_seconds
                > pickup_arrival_seconds + contract.days_to_complete * 86_400
            ):
                continue
        suggested_contract_ids.add(contract.contract_id)
        if constraints.collateral_mode is CollateralMode.LOCKED:
            locked_collateral_units += contract.collateral_units
        elapsed_seconds = delivery_completion_seconds
        current_system_id = routable.destination_system_id
        visited_required_system_ids = newly_visited_required_system_ids
    return frozenset(suggested_contract_ids)


def _build_route_event_catalog(prepared: PreparedProblem) -> _RouteEventCatalog:
    """Translate business objects into the events that can appear in the route.

    Optional public contracts contribute a pickup and a delivery that CP-SAT may skip together.
    Already accepted shipments and required waypoint systems contribute mandatory events.
    """

    problem = prepared.problem
    constraints = problem.constraints
    optional_contract_ids = {routable.contract.contract_id for routable in problem.contracts}
    active_contract_ids = {
        shipment.contract.contract.contract_id for shipment in problem.active_shipments
    }
    duplicate_contract_ids = optional_contract_ids & active_contract_ids
    if duplicate_contract_ids:
        raise ValueError(
            f"contracts cannot be both optional and active: {sorted(duplicate_contract_ids)}"
        )

    events: list[_RouteEvent] = [
        _RouteEvent(
            node_id=_START_NODE_ID,
            label=_START,
            action_kind=None,
            contract_id=None,
            system_id=constraints.start_system_id,
            location_id=None,
        ),
        _RouteEvent(
            node_id=_END_NODE_ID,
            label=_END,
            action_kind=None,
            contract_id=None,
            system_id=constraints.terminal_system_id,
            location_id=None,
        ),
    ]

    def add_event(
        *,
        label: str,
        action_kind: ActionKind | None,
        contract_id: int | None,
        system_id: int,
        location_id: int | None,
        cargo_delta: int = 0,
        collateral_delta: int = 0,
        parcel_delta: int = 0,
        is_optional: bool = False,
    ) -> int:
        node_id = len(events)
        events.append(
            _RouteEvent(
                node_id=node_id,
                label=label,
                action_kind=action_kind,
                contract_id=contract_id,
                system_id=system_id,
                location_id=location_id,
                cargo_delta=cargo_delta,
                collateral_delta=collateral_delta,
                parcel_delta=parcel_delta,
                is_optional=is_optional,
            )
        )
        return node_id

    optional_pickup_node_by_contract_id: dict[int, int] = {}
    optional_delivery_node_by_contract_id: dict[int, int] = {}
    for routable in problem.contracts:
        contract = routable.contract
        contract_id = contract.contract_id
        optional_pickup_node_by_contract_id[contract_id] = add_event(
            label=f"pickup:{contract_id}",
            action_kind=ActionKind.PICKUP,
            contract_id=contract_id,
            system_id=routable.origin_system_id,
            location_id=contract.origin_location_id,
            cargo_delta=contract.volume_units,
            collateral_delta=contract.collateral_units,
            parcel_delta=1,
            is_optional=True,
        )
        optional_delivery_node_by_contract_id[contract_id] = add_event(
            label=f"delivery:{contract_id}",
            action_kind=ActionKind.DELIVERY,
            contract_id=contract_id,
            system_id=routable.destination_system_id,
            location_id=contract.destination_location_id,
            cargo_delta=-contract.volume_units,
            collateral_delta=-contract.collateral_units,
            parcel_delta=-1,
            is_optional=True,
        )

    active_pickup_node_by_contract_id: dict[int, int] = {}
    active_delivery_node_by_contract_id: dict[int, int] = {}
    for shipment in problem.active_shipments:
        routable = shipment.contract
        contract = routable.contract
        contract_id = contract.contract_id
        if not shipment.picked:
            active_pickup_node_by_contract_id[contract_id] = add_event(
                label=f"committed-pickup:{contract_id}",
                action_kind=ActionKind.PICKUP,
                contract_id=contract_id,
                system_id=routable.origin_system_id,
                location_id=contract.origin_location_id,
                cargo_delta=contract.volume_units,
                # The contract was accepted earlier, so its collateral is already locked.
                collateral_delta=0,
                parcel_delta=1,
            )
        active_delivery_node_by_contract_id[contract_id] = add_event(
            label=f"active-delivery:{contract_id}",
            action_kind=ActionKind.DELIVERY,
            contract_id=contract_id,
            system_id=routable.destination_system_id,
            location_id=contract.destination_location_id,
            cargo_delta=-contract.volume_units,
            collateral_delta=-contract.collateral_units,
            parcel_delta=-1,
        )

    required_waypoint_node_ids: list[int] = []
    waypoint_already_guaranteed = {constraints.start_system_id}
    if constraints.terminal_system_id is not None:
        waypoint_already_guaranteed.add(constraints.terminal_system_id)
    for system_id in sorted(constraints.required_system_ids - waypoint_already_guaranteed):
        required_waypoint_node_ids.append(
            add_event(
                label=f"waypoint:{system_id}",
                action_kind=None,
                contract_id=None,
                system_id=system_id,
                location_id=None,
            )
        )

    return _RouteEventCatalog(
        events=tuple(events),
        optional_pickup_node_by_contract_id=optional_pickup_node_by_contract_id,
        optional_delivery_node_by_contract_id=optional_delivery_node_by_contract_id,
        active_pickup_node_by_contract_id=active_pickup_node_by_contract_id,
        active_delivery_node_by_contract_id=active_delivery_node_by_contract_id,
        required_waypoint_node_ids=tuple(required_waypoint_node_ids),
    )


def _build_model(prepared: PreparedProblem) -> _RouteModel:
    """Build the complete event-level CP-SAT model.

    Construction proceeds in five stages: enumerate route events, connect feasible event arcs,
    create state variables, propagate state along chosen arcs, and add contract-specific rules.
    """

    problem = prepared.problem
    constraints = problem.constraints
    event_catalog = _build_route_event_catalog(prepared)
    events = event_catalog.events
    optional_pickup_node_by_contract_id = event_catalog.optional_pickup_node_by_contract_id
    optional_delivery_node_by_contract_id = event_catalog.optional_delivery_node_by_contract_id
    active_pickup_node_by_contract_id = event_catalog.active_pickup_node_by_contract_id
    active_delivery_node_by_contract_id = event_catalog.active_delivery_node_by_contract_id
    required_waypoint_node_ids = event_catalog.required_waypoint_node_ids
    optional_contract_by_id = {
        routable.contract.contract_id: routable for routable in problem.contracts
    }
    active_shipment_by_id = {
        shipment.contract.contract.contract_id: shipment for shipment in problem.active_shipments
    }

    # Stage 1: contract-selection variables and the route circuit.
    model = cp_model.CpModel()
    contract_is_selected = {
        routable.contract.contract_id: model.new_bool_var(f"select_{routable.contract.contract_id}")
        for routable in problem.contracts
    }
    arc_is_used: dict[tuple[int, int], cp_model.IntVar] = {}
    circuit_arc_definitions: list[tuple[int, int, cp_model.IntVar]] = []

    # AddCircuit expects a cycle. This always-on artificial arc turns the desired start-to-end
    # path into a cycle without representing real travel or consuming time.
    end_to_start_arc_is_used = model.new_bool_var("end_to_start")
    model.add(end_to_start_arc_is_used == 1)
    arc_is_used[(_END_NODE_ID, _START_NODE_ID)] = end_to_start_arc_is_used
    circuit_arc_definitions.append((_END_NODE_ID, _START_NODE_ID, end_to_start_arc_is_used))

    for event in events[2:]:
        if event.is_optional:
            assert event.contract_id is not None
            event_is_skipped = model.new_bool_var(f"skip_{event.label}")
            model.add(event_is_skipped + contract_is_selected[event.contract_id] == 1)
            circuit_arc_definitions.append((event.node_id, event.node_id, event_is_skipped))
        else:
            # A false self-loop registers the mandatory node with AddCircuit even when every real
            # incoming/outgoing arc is eliminated as impossible. The model then proves infeasible
            # instead of accidentally omitting an unreachable commitment.
            mandatory_event_self_loop = model.new_bool_var(f"forbid_skip_{event.label}")
            model.add(mandatory_event_self_loop == 0)
            circuit_arc_definitions.append(
                (event.node_id, event.node_id, mandatory_event_self_loop)
            )

    def travel_time_between_events(
        from_event: _RouteEvent,
        to_event: _RouteEvent,
    ) -> int | None:
        if to_event.node_id == _END_NODE_ID and to_event.system_id is None:
            return 0
        if from_event.system_id is None or to_event.system_id is None:
            return None
        jump_count = prepared.jump_matrix.get((from_event.system_id, to_event.system_id))
        if jump_count is None:
            return None
        return jump_count * constraints.travel.seconds_per_jump

    route_horizon_seconds = constraints.horizon_seconds
    service_time_seconds = constraints.travel.service_seconds
    seconds_per_jump = constraints.travel.seconds_per_jump

    pickup_node_by_delivery_node_id = {
        optional_delivery_node_by_contract_id[contract_id]: (
            optional_pickup_node_by_contract_id[contract_id]
        )
        for contract_id in optional_pickup_node_by_contract_id
    }
    pickup_node_by_delivery_node_id.update(
        {
            active_delivery_node_by_contract_id[contract_id]: (
                active_pickup_node_by_contract_id[contract_id]
            )
            for contract_id in active_pickup_node_by_contract_id
        }
    )
    delivery_node_by_pickup_node_id = {
        pickup_node_id: delivery_node_id
        for delivery_node_id, pickup_node_id in pickup_node_by_delivery_node_id.items()
    }

    def direct_travel_time_seconds(
        source_system_id: int,
        destination_system_id: int,
    ) -> int | None:
        jump_count = prepared.jump_matrix.get((source_system_id, destination_system_id))
        return None if jump_count is None else jump_count * seconds_per_jump

    def earliest_possible_arrival(event: _RouteEvent) -> int:
        if event.node_id == _START_NODE_ID or event.system_id is None:
            return 0
        direct_travel_time = direct_travel_time_seconds(
            constraints.start_system_id,
            event.system_id,
        )
        if (
            event.action_kind is not ActionKind.DELIVERY
            or event.node_id not in pickup_node_by_delivery_node_id
        ):
            return route_horizon_seconds + 1 if direct_travel_time is None else direct_travel_time

        # An unpicked contract cannot reach its delivery before first reaching and servicing its
        # pickup.  This is a stronger lower bound than start->delivery and remains exact/safe even
        # when the eventual route interleaves other contracts.
        pickup_event = events[pickup_node_by_delivery_node_id[event.node_id]]
        assert pickup_event.system_id is not None
        travel_to_pickup_seconds = direct_travel_time_seconds(
            constraints.start_system_id,
            pickup_event.system_id,
        )
        travel_to_delivery_seconds = direct_travel_time_seconds(
            pickup_event.system_id,
            event.system_id,
        )
        if travel_to_pickup_seconds is None or travel_to_delivery_seconds is None:
            return route_horizon_seconds + 1
        return travel_to_pickup_seconds + service_time_seconds + travel_to_delivery_seconds

    def latest_allowed_arrival(event: _RouteEvent) -> int:
        if event.action_kind is None:
            return route_horizon_seconds
        latest_arrival_seconds = route_horizon_seconds - service_time_seconds
        assert event.contract_id is not None
        optional_contract = optional_contract_by_id.get(event.contract_id)
        if optional_contract is not None:
            if event.action_kind is ActionKind.PICKUP:
                minimum_delivery_travel_seconds = direct_travel_time_seconds(
                    optional_contract.origin_system_id,
                    optional_contract.destination_system_id,
                )
                if minimum_delivery_travel_seconds is not None:
                    latest_arrival_seconds = min(
                        latest_arrival_seconds,
                        route_horizon_seconds
                        - 2 * service_time_seconds
                        - minimum_delivery_travel_seconds,
                    )
                    if constraints.collateral_mode is CollateralMode.LOCKED:
                        latest_arrival_seconds = min(
                            latest_arrival_seconds,
                            optional_contract.contract.days_to_complete * 86_400
                            - 2 * service_time_seconds
                            - minimum_delivery_travel_seconds,
                        )
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    latest_arrival_seconds = min(
                        latest_arrival_seconds,
                        _last_valid_pickup_second(prepared, event.contract_id),
                    )
            if (
                event.action_kind is ActionKind.DELIVERY
                and constraints.collateral_mode is CollateralMode.LOCKED
            ):
                latest_arrival_seconds = min(
                    latest_arrival_seconds,
                    optional_contract.contract.days_to_complete * 86_400 - service_time_seconds,
                )
            return latest_arrival_seconds
        active_shipment = active_shipment_by_id[event.contract_id]
        deadline_seconds = int(
            (active_shipment.deadline - constraints.snapshot_time).total_seconds()
        )
        if event.action_kind is ActionKind.DELIVERY:
            return min(latest_arrival_seconds, deadline_seconds - service_time_seconds)
        minimum_delivery_travel_seconds = direct_travel_time_seconds(
            active_shipment.contract.origin_system_id,
            active_shipment.contract.destination_system_id,
        )
        if minimum_delivery_travel_seconds is not None:
            latest_arrival_seconds = min(
                latest_arrival_seconds,
                route_horizon_seconds - 2 * service_time_seconds - minimum_delivery_travel_seconds,
                deadline_seconds - 2 * service_time_seconds - minimum_delivery_travel_seconds,
            )
        return latest_arrival_seconds

    for source_event in events:
        if source_event.node_id == _END_NODE_ID:
            continue
        for destination_event in events:
            if (
                destination_event.node_id == _START_NODE_ID
                or destination_event.node_id == source_event.node_id
            ):
                continue
            if (
                (
                    source_event.node_id == _START_NODE_ID
                    and destination_event.node_id in pickup_node_by_delivery_node_id
                )
                or (
                    source_event.node_id in delivery_node_by_pickup_node_id
                    and destination_event.node_id == _END_NODE_ID
                )
                or pickup_node_by_delivery_node_id.get(source_event.node_id)
                == destination_event.node_id
            ):
                # These arcs violate the required pickup-before-delivery order: delivery
                # cannot be first, pickup cannot be last, and a delivery cannot point back to its
                # own pickup. Omitting them removes no feasible circuit.
                continue
            travel_time_seconds = travel_time_between_events(
                source_event,
                destination_event,
            )
            if travel_time_seconds is None:
                continue
            source_service_time_seconds = (
                service_time_seconds if source_event.action_kind is not None else 0
            )
            if earliest_possible_arrival(
                source_event
            ) + source_service_time_seconds + travel_time_seconds > latest_allowed_arrival(
                destination_event
            ):
                # Even the impossible-to-beat direct lower bound cannot reach the destination's
                # hard time window. Omitting this arc is therefore proof-preserving.
                continue
            is_arc_used = model.new_bool_var(
                f"arc_{source_event.node_id}_{destination_event.node_id}"
            )
            arc_is_used[(source_event.node_id, destination_event.node_id)] = is_arc_used
            circuit_arc_definitions.append(
                (source_event.node_id, destination_event.node_id, is_arc_used)
            )
    model.add_circuit(circuit_arc_definitions)

    # Stage 2: state at each event. Arc constraints below carry this state along the chosen route.
    arrival_time_seconds = [
        model.new_int_var(0, route_horizon_seconds, f"arrival_{event.node_id}") for event in events
    ]
    visit_order = [model.new_int_var(0, len(events), f"order_{event.node_id}") for event in events]
    model.add(arrival_time_seconds[_START_NODE_ID] == 0)
    model.add(visit_order[_START_NODE_ID] == 0)

    # Make route-implied event windows explicit so presolve/propagation need not rediscover them
    # through the circuit. Optional bounds apply only when the contract is selected.
    for routable in problem.contracts:
        contract_id = routable.contract.contract_id
        is_contract_selected = contract_is_selected[contract_id]
        contract_event_node_ids = (
            optional_pickup_node_by_contract_id[contract_id],
            optional_delivery_node_by_contract_id[contract_id],
        )
        for node_id in contract_event_node_ids:
            event = events[node_id]
            model.add(
                arrival_time_seconds[node_id] >= earliest_possible_arrival(event)
            ).only_enforce_if(is_contract_selected)
            model.add(
                arrival_time_seconds[node_id] <= latest_allowed_arrival(event)
            ).only_enforce_if(is_contract_selected)
    for node_id in (
        *active_pickup_node_by_contract_id.values(),
        *active_delivery_node_by_contract_id.values(),
        *required_waypoint_node_ids,
    ):
        event = events[node_id]
        model.add(arrival_time_seconds[node_id] >= earliest_possible_arrival(event))
        model.add(arrival_time_seconds[node_id] <= latest_allowed_arrival(event))

    cargo_capacity_units = constraints.cargo_capacity_units
    initial_cargo_load_units = sum(
        shipment.contract.contract.volume_units
        for shipment in problem.active_shipments
        if shipment.picked
    )
    cargo_load_units = [
        model.new_int_var(0, cargo_capacity_units, f"cargo_{event.node_id}") for event in events
    ]
    model.add(cargo_load_units[_START_NODE_ID] == initial_cargo_load_units)
    model.add(cargo_load_units[_END_NODE_ID] == 0)

    active_parcel_count: list[cp_model.IntVar] | None = None
    if constraints.max_simultaneous_contracts is not None:
        max_active_parcels = constraints.max_simultaneous_contracts
        initial_active_parcel_count = sum(
            1 for shipment in problem.active_shipments if shipment.picked
        )
        active_parcel_count = [
            model.new_int_var(0, max_active_parcels, f"parcels_{event.node_id}") for event in events
        ]
        model.add(active_parcel_count[_START_NODE_ID] == initial_active_parcel_count)
        model.add(active_parcel_count[_END_NODE_ID] == 0)

    locked_collateral_units: list[cp_model.IntVar] | None = None
    initial_locked_collateral_units = sum(
        shipment.contract.contract.collateral_units for shipment in problem.active_shipments
    )
    if constraints.collateral_mode is CollateralMode.ROLLING:
        locked_collateral_units = [
            model.new_int_var(
                0,
                constraints.collateral_budget_units,
                f"collateral_{event.node_id}",
            )
            for event in events
        ]
        model.add(locked_collateral_units[_START_NODE_ID] == initial_locked_collateral_units)
        model.add(locked_collateral_units[_END_NODE_ID] == 0)
    else:
        model.add(
            initial_locked_collateral_units
            + sum(
                routable.contract.collateral_units
                * contract_is_selected[routable.contract.contract_id]
                for routable in problem.contracts
            )
            <= constraints.collateral_budget_units
        )

    mandatory_action_count = sum(
        1 if shipment.picked else 2 for shipment in problem.active_shipments
    )
    if service_time_seconds > 0:
        # Every selected optional contract contributes exactly two serviced actions. This simple
        # route-independent inequality is redundant with exact time propagation but materially
        # strengthens the relaxation on dense candidate sets.
        model.add(
            service_time_seconds * (mandatory_action_count + 2 * sum(contract_is_selected.values()))
            <= route_horizon_seconds
        )

    # Stage 3: propagate time and resources over every chosen route arc.
    route_travel_time_terms: list[cp_model.LinearExpr] = []
    for (source_node_id, destination_node_id), is_arc_used in arc_is_used.items():
        if (source_node_id, destination_node_id) == (
            _END_NODE_ID,
            _START_NODE_ID,
        ):
            continue
        source_event = events[source_node_id]
        destination_event = events[destination_node_id]
        travel_time_seconds = travel_time_between_events(
            source_event,
            destination_event,
        )
        assert travel_time_seconds is not None
        if travel_time_seconds:
            route_travel_time_terms.append(travel_time_seconds * is_arc_used)
        source_service_time_seconds = (
            service_time_seconds if source_event.action_kind is not None else 0
        )
        model.add(
            arrival_time_seconds[destination_node_id]
            == arrival_time_seconds[source_node_id]
            + source_service_time_seconds
            + travel_time_seconds
        ).only_enforce_if(is_arc_used)
        model.add(
            visit_order[destination_node_id] == visit_order[source_node_id] + 1
        ).only_enforce_if(is_arc_used)
        model.add(
            cargo_load_units[destination_node_id]
            == cargo_load_units[source_node_id] + destination_event.cargo_delta
        ).only_enforce_if(is_arc_used)
        if active_parcel_count is not None:
            model.add(
                active_parcel_count[destination_node_id]
                == active_parcel_count[source_node_id] + destination_event.parcel_delta
            ).only_enforce_if(is_arc_used)
        if locked_collateral_units is not None:
            model.add(
                locked_collateral_units[destination_node_id]
                == locked_collateral_units[source_node_id] + destination_event.collateral_delta
            ).only_enforce_if(is_arc_used)

    # This equality follows from event-by-event arrival propagation for every complete route,
    # but exposing the complete travel budget in one linear row gives CP-SAT's relaxation a direct
    # link between selecting reward, paying service time, and paying the chosen arc distances.
    model.add(
        arrival_time_seconds[_END_NODE_ID]
        == sum(route_travel_time_terms)
        + service_time_seconds * (mandatory_action_count + 2 * sum(contract_is_selected.values()))
    )

    # Stage 4: pickup-before-delivery precedence and contract deadlines.
    for routable in problem.contracts:
        contract = routable.contract
        is_contract_selected = contract_is_selected[contract.contract_id]
        pickup_node_id = optional_pickup_node_by_contract_id[contract.contract_id]
        delivery_node_id = optional_delivery_node_by_contract_id[contract.contract_id]
        model.add(visit_order[delivery_node_id] >= visit_order[pickup_node_id] + 1).only_enforce_if(
            is_contract_selected
        )
        minimum_delivery_travel_seconds = direct_travel_time_seconds(
            routable.origin_system_id,
            routable.destination_system_id,
        )
        assert minimum_delivery_travel_seconds is not None
        # Whatever is interleaved between this pickup and delivery cannot beat the metric-closure
        # shortest path. Exposing this implied inequality directly gives CP-SAT substantially
        # stronger time propagation than relying on a chain of conditional route arcs alone.
        model.add(
            arrival_time_seconds[delivery_node_id]
            >= arrival_time_seconds[pickup_node_id]
            + service_time_seconds
            + minimum_delivery_travel_seconds
        ).only_enforce_if(is_contract_selected)
        if constraints.collateral_mode is CollateralMode.LOCKED:
            model.add(
                arrival_time_seconds[delivery_node_id] + service_time_seconds
                <= contract.days_to_complete * 86_400
            ).only_enforce_if(is_contract_selected)
        else:
            # Acceptance at the exact expiry timestamp is already too late. Arrival variables are
            # integral seconds, so ceil(delta)-1 is the greatest strictly-before expiry value.
            latest_pickup_second = _last_valid_pickup_second(prepared, contract.contract_id)
            model.add(arrival_time_seconds[pickup_node_id] <= latest_pickup_second).only_enforce_if(
                is_contract_selected
            )
            model.add(
                arrival_time_seconds[delivery_node_id] + service_time_seconds
                <= arrival_time_seconds[pickup_node_id] + contract.days_to_complete * 86_400
            ).only_enforce_if(is_contract_selected)

    for shipment in problem.active_shipments:
        contract_id = shipment.contract.contract.contract_id
        delivery_node_id = active_delivery_node_by_contract_id[contract_id]
        if not shipment.picked:
            pickup_node_id = active_pickup_node_by_contract_id[contract_id]
            model.add(visit_order[delivery_node_id] >= visit_order[pickup_node_id] + 1)
            minimum_delivery_travel_seconds = direct_travel_time_seconds(
                shipment.contract.origin_system_id,
                shipment.contract.destination_system_id,
            )
            if minimum_delivery_travel_seconds is not None:
                model.add(
                    arrival_time_seconds[delivery_node_id]
                    >= arrival_time_seconds[pickup_node_id]
                    + service_time_seconds
                    + minimum_delivery_travel_seconds
                )
        delivery_deadline_seconds = int(
            (shipment.deadline - constraints.snapshot_time).total_seconds()
        )
        model.add(
            arrival_time_seconds[delivery_node_id] + service_time_seconds
            <= delivery_deadline_seconds
        )

    # Stage 5: maximize reward. Hints suggest a feasible starting point but never restrict search.
    committed_reward_units = sum(
        shipment.contract.contract.reward_units for shipment in problem.active_shipments
    )
    maximum_reward_units = committed_reward_units + sum(
        routable.contract.reward_units for routable in problem.contracts
    )
    total_reward_units = model.new_int_var(
        committed_reward_units,
        maximum_reward_units,
        "total_reward",
    )
    model.add(
        total_reward_units
        == committed_reward_units
        + sum(
            routable.contract.reward_units * contract_is_selected[routable.contract.contract_id]
            for routable in problem.contracts
        )
    )
    model.maximize(total_reward_units)
    suggested_contract_ids = _build_greedy_selection_hint(prepared)
    for contract_id, is_contract_selected in contract_is_selected.items():
        model.add_hint(
            is_contract_selected,
            int(contract_id in suggested_contract_ids),
        )

    return _RouteModel(
        model=model,
        events=tuple(events),
        start_node_id=_START_NODE_ID,
        end_node_id=_END_NODE_ID,
        contract_is_selected=contract_is_selected,
        arc_is_used=arc_is_used,
        total_reward_units=total_reward_units,
        finish_time_seconds=arrival_time_seconds[_END_NODE_ID],
    )


def _read_solver_run_stats(
    solver: cp_model.CpSolver,
    status: cp_model.CpSolverStatus,
    objective_variable: cp_model.IntVar,
) -> _SolverRunStats:
    """Read exact integer objective data and search metrics from a completed solve."""

    status_name = solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _SolverRunStats(
            status=status,
            status_name=status_name,
            objective_units=None,
            bound_units=None,
            wall_time_seconds=solver.wall_time,
            branches=solver.num_branches,
            conflicts=solver.num_conflicts,
        )
    # Read the modeled integer directly. ``objective_value`` is exposed as a double and can lose
    # unit precision above 2**53, while ``value(IntVar)`` preserves the exact CP-SAT integer.
    objective_units = int(solver.value(objective_variable))
    if status == cp_model.OPTIMAL:
        upper_bound_units = objective_units
    else:
        # For a maximization problem CP-SAT's best objective bound is a rigorous upper bound.
        # The Python API exposes that bound as a double. Integers through 2**53-1 are represented
        # exactly; above that, widen by one floating ULP before taking the ceiling so a conversion
        # roundoff can never make the reported integer upper bound spuriously too small.
        solver_upper_bound = solver.best_objective_bound
        if abs(solver_upper_bound) <= 2**53 - 1:
            upper_bound_units = int(math.ceil(solver_upper_bound))
        else:
            upper_bound_units = int(math.ceil(math.nextafter(solver_upper_bound, math.inf)))
        upper_bound_units = max(upper_bound_units, objective_units)
    return _SolverRunStats(
        status=status,
        status_name=status_name,
        objective_units=objective_units,
        bound_units=upper_bound_units,
        wall_time_seconds=solver.wall_time,
        branches=solver.num_branches,
        conflicts=solver.num_conflicts,
    )


def _extract_visits(
    route_model: _RouteModel,
    solver: cp_model.CpSolver,
) -> tuple[tuple[PlannedVisit, ...], tuple[int, ...]]:
    selected_contract_ids = tuple(
        sorted(
            contract_id
            for contract_id, is_contract_selected in route_model.contract_is_selected.items()
            if solver.value(is_contract_selected)
        )
    )
    successor_node_by_node_id: dict[int, int] = {}
    for (source_node_id, destination_node_id), is_arc_used in route_model.arc_is_used.items():
        if solver.value(is_arc_used):
            successor_node_by_node_id[source_node_id] = destination_node_id
    visits: list[PlannedVisit] = []
    current_node_id = route_model.start_node_id
    visited_node_ids = {current_node_id}
    while True:
        next_node_id = successor_node_by_node_id.get(current_node_id)
        if next_node_id is None:
            raise RuntimeError(f"solver route has no successor for node {current_node_id}")
        if next_node_id == route_model.end_node_id:
            break
        if next_node_id in visited_node_ids:
            raise RuntimeError("solver route contains a cycle before the end node")
        visited_node_ids.add(next_node_id)
        event = route_model.events[next_node_id]
        if event.action_kind is None:
            if event.system_id is None or event.contract_id is not None:
                raise RuntimeError("unexpected anonymous event inside solver route")
            visits.append(PlannedWaypoint(event.system_id))
        else:
            if event.contract_id is None:
                raise RuntimeError("contract action is missing its contract ID")
            visits.append(PlannedAction(event.action_kind, event.contract_id))
        current_node_id = next_node_id
    return tuple(visits), selected_contract_ids


def _build_dense_selection_cuts(prepared: PreparedProblem) -> SelectionCuts:
    if len(prepared.problem.contracts) < _BOUND_STRENGTHENING_MIN_CONTRACTS:
        return SelectionCuts((), ())
    return build_selection_cuts(prepared)


def _restrict_to_contract_selection(
    prepared: PreparedProblem,
    selected_contract_ids: tuple[int, ...],
) -> PreparedProblem:
    """Keep only one optional contract set while preserving every mandatory route constraint.

    Optional courier feasibility is downward-closed. Removing optional pickup/delivery pairs can
    only remove service time, cargo, collateral, and parcel load. The jump matrix is a metric
    closure, so shortcutting deleted actions never lengthens travel. Mandatory waypoints, active
    shipments and the required finish remain in the problem. Therefore, if a reduced set C is
    infeasible, every full-universe selection containing C is infeasible too.
    """

    selected_contract_id_set = frozenset(selected_contract_ids)
    known_contract_ids = {contract.contract.contract_id for contract in prepared.problem.contracts}
    unknown_contract_ids = tuple(sorted(selected_contract_id_set - known_contract_ids))
    if unknown_contract_ids:
        raise ValueError(
            f"reduced exact selection contains unknown contract IDs: {unknown_contract_ids}"
        )
    selected_contracts = tuple(
        contract
        for contract in prepared.problem.contracts
        if contract.contract.contract_id in selected_contract_id_set
    )
    selected_contract_scores = tuple(
        score
        for score in prepared.scores
        if score.contract.contract.contract_id in selected_contract_id_set
    )
    return PreparedProblem(
        problem=replace(prepared.problem, contracts=selected_contracts),
        jump_matrix=prepared.jump_matrix,
        scores=selected_contract_scores,
    )


def _new_time_limited_solver(config: SolverConfig, max_time_seconds: float) -> cp_model.CpSolver:
    """Create a solver for one bounded decomposition subproblem."""

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    solver.parameters.num_search_workers = config.num_workers
    solver.parameters.random_seed = config.random_seed
    solver.parameters.log_search_progress = config.log_search_progress
    return solver


def _solve_reduced_exact_oracle(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    selected_contract_ids: tuple[int, ...],
    config: SolverConfig,
    *,
    max_time_seconds: float,
) -> _ExactOracleResult:
    """Test one master selection exactly and return a sufficient infeasibility core when needed."""

    reduced_problem = _restrict_to_contract_selection(prepared, selected_contract_ids)
    route_model = _build_model(reduced_problem)
    route_model.model.maximize(0)
    contract_id_by_assumption_index = {
        literal.index: contract_id
        for contract_id, literal in route_model.contract_is_selected.items()
    }
    solve_deadline = time.perf_counter() + max_time_seconds
    total_wall_time_seconds = 0.0
    total_branches = 0
    total_conflicts = 0

    def solve_assuming_selected_contracts(
        assumed_selected_contract_ids: tuple[int, ...],
        time_limit_seconds: float,
    ) -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus]:
        route_model.model.clear_assumptions()
        route_model.model.add_assumptions(
            [
                route_model.contract_is_selected[contract_id]
                for contract_id in assumed_selected_contract_ids
            ]
        )
        validation_error = route_model.model.validate()
        if validation_error:
            raise ValueError(f"invalid reduced exact model: {validation_error}")
        solver = _new_time_limited_solver(config, time_limit_seconds)
        status = solver.solve(route_model.model)
        if status == cp_model.MODEL_INVALID:
            raise ValueError("CP-SAT rejected the validated reduced exact model")
        return solver, status

    selection_solver, status = solve_assuming_selected_contracts(
        selected_contract_ids, max_time_seconds
    )
    total_wall_time_seconds += selection_solver.wall_time
    total_branches += selection_solver.num_branches
    total_conflicts += selection_solver.num_conflicts
    status_name = selection_solver.status_name(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        visits, solved_contract_ids = _extract_visits(route_model, selection_solver)
        expected_contract_ids = tuple(sorted(selected_contract_ids))
        if solved_contract_ids != expected_contract_ids:
            raise RuntimeError(
                f"reduced exact oracle did not enforce its selection: {solved_contract_ids}"
            )
        simulation = simulate_and_verify(prepared.problem, graph, visits, solved_contract_ids)
        if not simulation.report.valid:
            raise RuntimeError(
                "reduced exact route failed independent full-problem verification: "
                + "; ".join(simulation.report.violations)
            )
        return _ExactOracleResult(
            status=status,
            status_name=status_name,
            selected_contract_ids=solved_contract_ids,
            simulation=simulation,
            infeasible_core_ids=(),
            wall_time_seconds=total_wall_time_seconds,
            branches=total_branches,
            conflicts=total_conflicts,
        )
    if status != cp_model.INFEASIBLE:
        return _ExactOracleResult(
            status=status,
            status_name=status_name,
            selected_contract_ids=(),
            simulation=None,
            infeasible_core_ids=(),
            wall_time_seconds=total_wall_time_seconds,
            branches=total_branches,
            conflicts=total_conflicts,
        )

    assumption_core_indexes = selection_solver.sufficient_assumptions_for_infeasibility()
    unexpected_assumption_indexes = tuple(
        index for index in assumption_core_indexes if index not in contract_id_by_assumption_index
    )
    if unexpected_assumption_indexes:
        raise RuntimeError(
            f"CP-SAT returned unexpected assumption literals: {unexpected_assumption_indexes}"
        )
    infeasible_contract_ids = tuple(
        sorted(contract_id_by_assumption_index[index] for index in assumption_core_indexes)
    )

    # CP-SAT promises a sufficient core, not a minimal one. Deletion checks can make the learned
    # master cut substantially stronger. A literal is removed only after another exact INFEASIBLE
    # result, so an UNKNOWN shrink attempt can never make the proof unsafe.
    for contract_id in tuple(infeasible_contract_ids):
        if contract_id not in infeasible_contract_ids:
            continue
        remaining_time_seconds = solve_deadline - time.perf_counter()
        if remaining_time_seconds <= 0.001:
            break
        trial_contract_ids = tuple(
            candidate_contract_id
            for candidate_contract_id in infeasible_contract_ids
            if candidate_contract_id != contract_id
        )
        shrink_solver, shrink_status = solve_assuming_selected_contracts(
            trial_contract_ids, remaining_time_seconds
        )
        total_wall_time_seconds += shrink_solver.wall_time
        total_branches += shrink_solver.num_branches
        total_conflicts += shrink_solver.num_conflicts
        if shrink_status != cp_model.INFEASIBLE:
            continue
        smaller_core_indexes = shrink_solver.sufficient_assumptions_for_infeasibility()
        unexpected_smaller_core_indexes = tuple(
            index for index in smaller_core_indexes if index not in contract_id_by_assumption_index
        )
        if unexpected_smaller_core_indexes:
            raise RuntimeError(
                "CP-SAT returned unexpected shrink-core literals: "
                f"{unexpected_smaller_core_indexes}"
            )
        infeasible_contract_ids = tuple(
            sorted(contract_id_by_assumption_index[index] for index in smaller_core_indexes)
        )

    return _ExactOracleResult(
        status=status,
        status_name=status_name,
        selected_contract_ids=(),
        simulation=None,
        infeasible_core_ids=infeasible_contract_ids,
        wall_time_seconds=total_wall_time_seconds,
        branches=total_branches,
        conflicts=total_conflicts,
    )


def _refine_fixed_selection_finish_time(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    selected_contract_ids: tuple[int, ...],
    config: SolverConfig,
) -> _ExactOracleResult:
    """Optionally minimize finish time after the decomposition has already proved max reward."""

    reduced_problem = _restrict_to_contract_selection(prepared, selected_contract_ids)
    route_model = _build_model(reduced_problem)
    for contract_id in selected_contract_ids:
        route_model.model.add(route_model.contract_is_selected[contract_id] == 1)
    route_model.model.minimize(route_model.finish_time_seconds)
    validation_error = route_model.model.validate()
    if validation_error:
        raise ValueError(f"invalid fixed-selection refinement model: {validation_error}")
    solver = _new_time_limited_solver(config, config.secondary_time_seconds)
    status = solver.solve(route_model.model)
    if status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT rejected the validated fixed-selection refinement model")
    simulation: SimulationResult | None = None
    solved_contract_ids: tuple[int, ...] = ()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        visits, solved_contract_ids = _extract_visits(route_model, solver)
        expected_contract_ids = tuple(sorted(selected_contract_ids))
        if solved_contract_ids != expected_contract_ids:
            raise RuntimeError(
                f"fixed-selection refinement changed its contract set: {solved_contract_ids}"
            )
        simulation = simulate_and_verify(prepared.problem, graph, visits, solved_contract_ids)
        if not simulation.report.valid:
            raise RuntimeError(
                "fixed-selection refinement failed independent verification: "
                + "; ".join(simulation.report.violations)
            )
    elif status == cp_model.INFEASIBLE:
        raise RuntimeError("fixed-selection refinement contradicted a verified feasible route")
    return _ExactOracleResult(
        status=status,
        status_name=solver.status_name(status),
        selected_contract_ids=solved_contract_ids,
        simulation=simulation,
        infeasible_core_ids=(),
        wall_time_seconds=solver.wall_time,
        branches=solver.num_branches,
        conflicts=solver.num_conflicts,
    )


def _run_dense_decomposition(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    config: SolverConfig,
) -> _DecompositionOutcome:
    """Try to prove the answer with a smaller model before building the full route model.

    The *master* model chooses contracts and routes between systems, but deliberately ignores the
    exact order of pickup and delivery actions within those systems. The exact route model then
    checks the master's proposed contract set. When that set is impossible, CP-SAT returns a
    smaller conflicting set of contracts. Adding that conflict to the master prevents it from
    making the same kind of impossible choice again.

    A feasible exact route whose reward equals the master's upper bound is globally optimal. If
    this loop runs out of time, ``solve_exact`` falls back to the complete route model and reuses
    every bound and conflict learned here.
    """

    selection_cuts = _build_dense_selection_cuts(prepared)
    if len(prepared.problem.contracts) < _BOUND_STRENGTHENING_MIN_CONTRACTS:
        return _DecompositionOutcome(
            relaxation=None,
            selection_cuts=selection_cuts,
            simulation=None,
            selected_contract_ids=(),
            proven_infeasible=False,
            status_name=None,
            iteration_count=0,
            learned_infeasibility_cores=(),
            subproblem_wall_time_seconds=0.0,
            subproblem_branches=0,
            subproblem_conflicts=0,
        )
    if config.relaxation_time_seconds <= 0:
        return _DecompositionOutcome(
            relaxation=None,
            selection_cuts=selection_cuts,
            simulation=None,
            selected_contract_ids=(),
            proven_infeasible=False,
            status_name="disabled",
            iteration_count=0,
            learned_infeasibility_cores=(),
            subproblem_wall_time_seconds=0.0,
            subproblem_branches=0,
            subproblem_conflicts=0,
        )

    master_model = build_system_relaxation_master(prepared, selection_cuts=selection_cuts)
    if config.decomposition_time_seconds == 0:
        system_relaxation = solve_system_relaxation_master(
            master_model,
            max_time_seconds=config.relaxation_time_seconds,
            random_seed=config.random_seed,
        )
        return _DecompositionOutcome(
            relaxation=system_relaxation,
            selection_cuts=selection_cuts,
            simulation=None,
            selected_contract_ids=(),
            proven_infeasible=False,
            status_name="bound_only",
            iteration_count=0,
            learned_infeasibility_cores=(),
            subproblem_wall_time_seconds=0.0,
            subproblem_branches=0,
            subproblem_conflicts=0,
        )

    decomposition_deadline = time.perf_counter() + config.decomposition_time_seconds
    learned_infeasibility_cores: list[tuple[int, ...]] = []
    latest_master_result: SystemRelaxationBound | None = None
    master_wall_time_seconds = 0.0
    master_branches = 0
    master_conflicts = 0
    subproblem_wall_time_seconds = 0.0
    subproblem_branches = 0
    subproblem_conflicts = 0
    iteration_count = 0
    status_name = "budget_exhausted"
    proven_infeasible = False
    verified_simulation: SimulationResult | None = None
    selected_contract_ids: tuple[int, ...] = ()

    for _ in range(config.decomposition_max_iterations):
        remaining_time_seconds = decomposition_deadline - time.perf_counter()
        if remaining_time_seconds <= 0.001:
            break
        iteration_count += 1

        master_result = solve_system_relaxation_master(
            master_model,
            max_time_seconds=min(config.relaxation_time_seconds, remaining_time_seconds),
            random_seed=config.random_seed,
        )
        master_wall_time_seconds += master_result.wall_time_seconds
        master_branches += master_result.branches
        master_conflicts += master_result.conflicts
        latest_master_result = master_result
        if master_result.status_name == "INFEASIBLE":
            proven_infeasible = True
            status_name = "master_infeasible"
            break
        if master_result.status_name != "OPTIMAL":
            status_name = f"master_{master_result.status_name.lower()}"
            break

        remaining_time_seconds = decomposition_deadline - time.perf_counter()
        if remaining_time_seconds <= 0.001:
            status_name = "budget_exhausted"
            break
        exact_route_result = _solve_reduced_exact_oracle(
            prepared,
            graph,
            master_result.selected_contract_ids,
            config,
            max_time_seconds=min(
                config.decomposition_subproblem_time_seconds,
                remaining_time_seconds,
            ),
        )
        subproblem_wall_time_seconds += exact_route_result.wall_time_seconds
        subproblem_branches += exact_route_result.branches
        subproblem_conflicts += exact_route_result.conflicts
        if exact_route_result.status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if exact_route_result.simulation is None or master_result.objective_units is None:
                raise RuntimeError("feasible reduced exact oracle is missing its verified solution")
            if exact_route_result.simulation.total_reward_units != master_result.objective_units:
                raise RuntimeError(
                    "master objective disagrees with the verified reduced exact route"
                )
            if master_result.upper_bound_units != exact_route_result.simulation.total_reward_units:
                raise RuntimeError("optimal system master returned an inconsistent objective bound")
            verified_simulation = exact_route_result.simulation
            selected_contract_ids = exact_route_result.selected_contract_ids
            status_name = "bound_matched"
            if config.minimize_finish_time_after_proof:
                refinement_result = _refine_fixed_selection_finish_time(
                    prepared,
                    graph,
                    selected_contract_ids,
                    config,
                )
                subproblem_wall_time_seconds += refinement_result.wall_time_seconds
                subproblem_branches += refinement_result.branches
                subproblem_conflicts += refinement_result.conflicts
                if (
                    refinement_result.simulation is not None
                    and refinement_result.simulation.finish_seconds
                    < verified_simulation.finish_seconds
                ):
                    verified_simulation = refinement_result.simulation
            break
        if exact_route_result.status != cp_model.INFEASIBLE:
            status_name = f"oracle_{exact_route_result.status_name.lower()}"
            break
        infeasible_contract_ids = exact_route_result.infeasible_core_ids
        if not infeasible_contract_ids:
            proven_infeasible = True
            status_name = "exact_base_infeasible"
            break
        if infeasible_contract_ids in learned_infeasibility_cores:
            raise RuntimeError("logic-based decomposition produced a duplicate infeasibility core")
        add_proven_infeasible_selection_cut(master_model, infeasible_contract_ids)
        learned_infeasibility_cores.append(infeasible_contract_ids)
        status_name = "core_learned"
    else:
        status_name = "iteration_limit"

    if latest_master_result is not None:
        latest_master_result = replace(
            latest_master_result,
            wall_time_seconds=master_wall_time_seconds,
            branches=master_branches,
            conflicts=master_conflicts,
        )
    return _DecompositionOutcome(
        relaxation=latest_master_result,
        selection_cuts=selection_cuts,
        simulation=verified_simulation,
        selected_contract_ids=selected_contract_ids,
        proven_infeasible=proven_infeasible,
        status_name=status_name,
        iteration_count=iteration_count,
        learned_infeasibility_cores=tuple(learned_infeasibility_cores),
        subproblem_wall_time_seconds=subproblem_wall_time_seconds,
        subproblem_branches=subproblem_branches,
        subproblem_conflicts=subproblem_conflicts,
    )


def solve_exact(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    *,
    config: SolverConfig | None = None,
) -> SolveResult:
    """Find the highest-reward valid route and return its proof metadata.

    The solve proceeds from cheapest to most expressive:

    1. Try the smaller system-level master and exact contract-set checks.
    2. If that does not close the proof, solve the complete pickup/delivery route model.
    3. Once maximum reward is proven, optionally minimize finish time without changing reward.
    4. Replay the route independently and, for small instances, compare it with exhaustive search.
    """

    solver_config = config or SolverConfig()

    # The decomposition often proves dense instances without constructing the full action model.
    decomposition = _run_dense_decomposition(prepared, graph, solver_config)
    relaxation = decomposition.relaxation
    selection_cuts = decomposition.selection_cuts
    problem_sha256 = canonical_problem_sha256(prepared.problem, prepared.jump_matrix)
    relaxation_wall_time = relaxation.wall_time_seconds if relaxation is not None else 0.0
    relaxation_branches = relaxation.branches if relaxation is not None else 0
    relaxation_conflicts = relaxation.conflicts if relaxation is not None else 0
    relaxation_status = relaxation.status_name if relaxation is not None else None
    relaxation_bound = relaxation.upper_bound_units if relaxation is not None else None
    relaxation_systems = relaxation.routed_systems if relaxation is not None else 0
    preprocessing_wall_time_seconds = (
        relaxation_wall_time + decomposition.subproblem_wall_time_seconds
    )
    preprocessing_branches = relaxation_branches + decomposition.subproblem_branches
    preprocessing_conflicts = relaxation_conflicts + decomposition.subproblem_conflicts

    if decomposition.simulation is not None:
        if relaxation_bound != decomposition.simulation.total_reward_units:
            raise RuntimeError("decomposition route does not meet the rigorous master bound")
        certificate = OptimalityCertificate(
            status=ProofStatus.PROVEN_OPTIMAL,
            solver_status="DECOMPOSITION_OPTIMAL",
            objective_units=decomposition.simulation.total_reward_units,
            best_bound_units=relaxation_bound,
            absolute_gap_units=0,
            relative_gap=0.0,
            problem_sha256=problem_sha256,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=preprocessing_wall_time_seconds,
            branches=preprocessing_branches,
            conflicts=preprocessing_conflicts,
            scope_untruncated=prepared.problem.scope.is_untruncated,
            feasibility_verified=True,
            independent_reference_verified=False,
            claim=optimality_claim(prepared.problem),
            system_relaxation_status=relaxation_status,
            system_relaxation_bound_units=relaxation_bound,
            system_relaxation_wall_time_seconds=relaxation_wall_time,
            system_relaxation_systems=relaxation_systems,
            incompatibility_pairs=len(selection_cuts.pairs),
            incompatibility_cliques=len(selection_cuts.cliques),
            decomposition_status=decomposition.status_name,
            decomposition_iterations=decomposition.iteration_count,
            decomposition_learned_cuts=len(decomposition.learned_infeasibility_cores),
            decomposition_subproblem_wall_time_seconds=(decomposition.subproblem_wall_time_seconds),
            decomposition_proof_closed=True,
        )
        return SolveResult(
            selected_contract_ids=decomposition.selected_contract_ids,
            route=decomposition.simulation.steps,
            total_reward_units=decomposition.simulation.total_reward_units,
            finish_seconds=decomposition.simulation.finish_seconds,
            certificate=certificate,
            travel_legs=decomposition.simulation.travel_legs,
        )

    if decomposition.proven_infeasible:
        certificate = OptimalityCertificate(
            status=ProofStatus.PROVEN_INFEASIBLE,
            solver_status="DECOMPOSITION_INFEASIBLE",
            objective_units=None,
            best_bound_units=None,
            absolute_gap_units=None,
            relative_gap=None,
            problem_sha256=problem_sha256,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=preprocessing_wall_time_seconds,
            branches=preprocessing_branches,
            conflicts=preprocessing_conflicts,
            scope_untruncated=prepared.problem.scope.is_untruncated,
            feasibility_verified=False,
            independent_reference_verified=False,
            claim=optimality_claim(prepared.problem),
            system_relaxation_status=relaxation_status,
            system_relaxation_bound_units=relaxation_bound,
            system_relaxation_wall_time_seconds=relaxation_wall_time,
            system_relaxation_systems=relaxation_systems,
            incompatibility_pairs=len(selection_cuts.pairs),
            incompatibility_cliques=len(selection_cuts.cliques),
            decomposition_status=decomposition.status_name,
            decomposition_iterations=decomposition.iteration_count,
            decomposition_learned_cuts=len(decomposition.learned_infeasibility_cores),
            decomposition_subproblem_wall_time_seconds=(decomposition.subproblem_wall_time_seconds),
            decomposition_proof_closed=True,
        )
        return SolveResult((), (), 0, 0, certificate)

    # The prepass did not finish the proof. Build the complete event-level route model, carrying
    # forward every rigorous upper bound and infeasible contract set it discovered.
    route_model = _build_model(prepared)
    _apply_bound_strengthening(
        route_model,
        relaxation,
        selection_cuts,
        decomposition.learned_infeasibility_cores,
    )
    validation_error = route_model.model.validate()
    if validation_error:
        raise ValueError(f"invalid or numerically unsafe CP-SAT model: {validation_error}")
    reward_solver = _new_solver(solver_config)
    reward_status = reward_solver.solve(route_model.model)
    reward_solve_stats = _read_solver_run_stats(
        reward_solver, reward_status, route_model.total_reward_units
    )

    if reward_status == cp_model.INFEASIBLE:
        certificate = OptimalityCertificate(
            status=ProofStatus.PROVEN_INFEASIBLE,
            solver_status=reward_solve_stats.status_name,
            objective_units=None,
            best_bound_units=None,
            absolute_gap_units=None,
            relative_gap=None,
            problem_sha256=problem_sha256,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=(
                preprocessing_wall_time_seconds + reward_solve_stats.wall_time_seconds
            ),
            branches=preprocessing_branches + reward_solve_stats.branches,
            conflicts=preprocessing_conflicts + reward_solve_stats.conflicts,
            scope_untruncated=prepared.problem.scope.is_untruncated,
            feasibility_verified=False,
            independent_reference_verified=False,
            claim=optimality_claim(prepared.problem),
            system_relaxation_status=relaxation_status,
            system_relaxation_bound_units=relaxation_bound,
            system_relaxation_wall_time_seconds=relaxation_wall_time,
            system_relaxation_systems=relaxation_systems,
            incompatibility_pairs=len(selection_cuts.pairs),
            incompatibility_cliques=len(selection_cuts.cliques),
            decomposition_status=decomposition.status_name,
            decomposition_iterations=decomposition.iteration_count,
            decomposition_learned_cuts=len(decomposition.learned_infeasibility_cores),
            decomposition_subproblem_wall_time_seconds=(decomposition.subproblem_wall_time_seconds),
            decomposition_proof_closed=False,
        )
        return SolveResult((), (), 0, 0, certificate)
    if reward_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        certificate = OptimalityCertificate(
            status=ProofStatus.UNKNOWN,
            solver_status=reward_solve_stats.status_name,
            objective_units=None,
            best_bound_units=relaxation_bound,
            absolute_gap_units=None,
            relative_gap=None,
            problem_sha256=problem_sha256,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=(
                preprocessing_wall_time_seconds + reward_solve_stats.wall_time_seconds
            ),
            branches=preprocessing_branches + reward_solve_stats.branches,
            conflicts=preprocessing_conflicts + reward_solve_stats.conflicts,
            scope_untruncated=prepared.problem.scope.is_untruncated,
            feasibility_verified=False,
            independent_reference_verified=False,
            claim=optimality_claim(prepared.problem),
            system_relaxation_status=relaxation_status,
            system_relaxation_bound_units=relaxation_bound,
            system_relaxation_wall_time_seconds=relaxation_wall_time,
            system_relaxation_systems=relaxation_systems,
            incompatibility_pairs=len(selection_cuts.pairs),
            incompatibility_cliques=len(selection_cuts.cliques),
            decomposition_status=decomposition.status_name,
            decomposition_iterations=decomposition.iteration_count,
            decomposition_learned_cuts=len(decomposition.learned_infeasibility_cores),
            decomposition_subproblem_wall_time_seconds=(decomposition.subproblem_wall_time_seconds),
            decomposition_proof_closed=False,
        )
        return SolveResult((), (), 0, 0, certificate)

    route_solution_solver = reward_solver
    total_wall_time_seconds = preprocessing_wall_time_seconds + reward_solve_stats.wall_time_seconds
    total_branches = preprocessing_branches + reward_solve_stats.branches
    total_conflicts = preprocessing_conflicts + reward_solve_stats.conflicts
    if reward_status == cp_model.OPTIMAL and solver_config.minimize_finish_time_after_proof:
        assert reward_solve_stats.objective_units is not None
        route_model.model.add(route_model.total_reward_units == reward_solve_stats.objective_units)
        route_model.model.minimize(route_model.finish_time_seconds)
        finish_time_solver = _new_solver(solver_config, use_secondary_time_limit=True)
        finish_time_status = finish_time_solver.solve(route_model.model)
        if finish_time_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            route_solution_solver = finish_time_solver
            total_wall_time_seconds += finish_time_solver.wall_time
            total_branches += finish_time_solver.num_branches
            total_conflicts += finish_time_solver.num_conflicts

    # A solver assignment is not trusted as a route until the independent simulator accepts it.
    planned_visits, selected_contract_ids = _extract_visits(route_model, route_solution_solver)
    verified_route = simulate_and_verify(
        prepared.problem,
        graph,
        planned_visits,
        selected_contract_ids,
    )
    if not verified_route.report.valid:
        raise RuntimeError(
            "solver returned a route that failed independent verification: "
            + "; ".join(verified_route.report.violations)
        )
    if (
        reward_solve_stats.objective_units is None
        or verified_route.total_reward_units != reward_solve_stats.objective_units
    ):
        raise RuntimeError("independent simulation reward does not match solver objective")

    # Exhaustive dynamic programming is affordable only for small, simple locked-collateral
    # instances, but provides a valuable implementation-independent objective check there.
    reference_verified = False
    if (
        reward_status == cp_model.OPTIMAL
        and prepared.problem.constraints.collateral_mode is CollateralMode.LOCKED
        and not prepared.problem.active_shipments
        and not prepared.problem.constraints.required_system_ids
        and len(prepared.problem.contracts) <= solver_config.independent_reference_limit
    ):
        reference_result = solve_reference(
            prepared,
            contract_limit=solver_config.independent_reference_limit,
        )
        if reference_result.objective_units != reward_solve_stats.objective_units:
            raise RuntimeError(
                "CP-SAT optimum disagrees with independent exhaustive reference solver: "
                f"{reward_solve_stats.objective_units} != {reference_result.objective_units}"
            )
        reference_verified = True

    assert reward_solve_stats.bound_units is not None
    absolute_gap_units = max(0, reward_solve_stats.bound_units - reward_solve_stats.objective_units)
    relative_gap = absolute_gap_units / max(1, abs(reward_solve_stats.objective_units))
    proof_status = (
        ProofStatus.PROVEN_OPTIMAL
        if reward_status == cp_model.OPTIMAL
        else ProofStatus.FEASIBLE_NOT_PROVEN
    )
    certificate = OptimalityCertificate(
        status=proof_status,
        solver_status=reward_solve_stats.status_name,
        objective_units=reward_solve_stats.objective_units,
        best_bound_units=reward_solve_stats.bound_units,
        absolute_gap_units=absolute_gap_units,
        relative_gap=relative_gap,
        problem_sha256=problem_sha256,
        solver_name="OR-Tools CP-SAT",
        solver_version=package_version("ortools"),
        wall_time_seconds=total_wall_time_seconds,
        branches=total_branches,
        conflicts=total_conflicts,
        scope_untruncated=prepared.problem.scope.is_untruncated,
        feasibility_verified=True,
        independent_reference_verified=reference_verified,
        claim=optimality_claim(prepared.problem),
        system_relaxation_status=relaxation_status,
        system_relaxation_bound_units=relaxation_bound,
        system_relaxation_wall_time_seconds=relaxation_wall_time,
        system_relaxation_systems=relaxation_systems,
        incompatibility_pairs=len(selection_cuts.pairs),
        incompatibility_cliques=len(selection_cuts.cliques),
        decomposition_status=decomposition.status_name,
        decomposition_iterations=decomposition.iteration_count,
        decomposition_learned_cuts=len(decomposition.learned_infeasibility_cores),
        decomposition_subproblem_wall_time_seconds=(decomposition.subproblem_wall_time_seconds),
        decomposition_proof_closed=False,
    )
    return SolveResult(
        selected_contract_ids=selected_contract_ids,
        route=verified_route.steps,
        total_reward_units=verified_route.total_reward_units,
        finish_seconds=verified_route.finish_seconds,
        certificate=certificate,
        travel_legs=verified_route.travel_legs,
    )
