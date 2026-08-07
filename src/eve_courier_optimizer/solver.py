"""Proof-capable CP-SAT solver for optional pickup-and-delivery routing."""

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
class _Event:
    node: int
    label: str
    action: ActionKind | None
    contract_id: int | None
    system_id: int | None
    location_id: int | None
    cargo_delta: int = 0
    collateral_delta: int = 0
    parcel_delta: int = 0
    optional: bool = False


@dataclass(slots=True)
class _ModelArtifacts:
    model: cp_model.CpModel
    events: tuple[_Event, ...]
    start_node: int
    end_node: int
    selected: dict[int, cp_model.IntVar]
    route_arcs: dict[tuple[int, int], cp_model.IntVar]
    total_reward: cp_model.IntVar
    finish_time: cp_model.IntVar


@dataclass(frozen=True, slots=True)
class _PrimaryStats:
    status: cp_model.CpSolverStatus
    status_name: str
    objective_units: int | None
    bound_units: int | None
    wall_time: float
    branches: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class _ExactOracleResult:
    status: cp_model.CpSolverStatus
    status_name: str
    selected_contract_ids: tuple[int, ...]
    simulation: SimulationResult | None
    infeasible_core_ids: tuple[int, ...]
    wall_time: float
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
    iterations: int
    learned_cuts: tuple[tuple[int, ...], ...]
    subproblem_wall_time: float
    subproblem_branches: int
    subproblem_conflicts: int


def _solver(config: SolverConfig, *, secondary: bool = False) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    if secondary:
        solver.parameters.max_time_in_seconds = config.secondary_time_seconds
    elif config.max_time_seconds is not None:
        solver.parameters.max_time_in_seconds = config.max_time_seconds
    solver.parameters.num_search_workers = config.num_workers
    solver.parameters.random_seed = config.random_seed
    solver.parameters.log_search_progress = config.log_search_progress
    return solver


def _apply_bound_strengthening(
    artifacts: _ModelArtifacts,
    relaxation: SystemRelaxationBound | None,
    cuts: SelectionCuts,
    learned_cuts: tuple[tuple[int, ...], ...] = (),
) -> None:
    """Attach only logically redundant reward and incompatibility constraints."""

    if relaxation is not None and relaxation.upper_bound_units is not None:
        artifacts.model.add(artifacts.total_reward <= relaxation.upper_bound_units)

    clique_pairs = {
        tuple(sorted(pair)) for clique in cuts.cliques for pair in combinations(clique, 2)
    }
    for clique in cuts.cliques:
        artifacts.model.add(sum(artifacts.selected[item] for item in clique) <= 1)
    for first, second in cuts.pairs:
        if (first, second) not in clique_pairs:
            artifacts.model.add(artifacts.selected[first] + artifacts.selected[second] <= 1)
    for core in learned_cuts:
        artifacts.model.add(sum(artifacts.selected[item] for item in core) <= len(core) - 1)


def _listing_last_second(prepared: PreparedProblem, contract_id: int) -> int:
    contract = next(
        item.contract
        for item in prepared.problem.contracts
        if item.contract.contract_id == contract_id
    )
    return max(
        0,
        math.ceil(
            (contract.date_expired - prepared.problem.constraints.snapshot_time).total_seconds()
        )
        - 1,
    )


def _greedy_hint_ids(prepared: PreparedProblem) -> frozenset[int]:
    """Build a deterministic sequential selection hint with a feasible required-route tail.

    Hints never constrain the model. This conservative constructor selects only jobs it can visit
    pickup-then-delivery without interleaving while still reserving a concrete path through every
    remaining required system and the terminal. CP-SAT remains free to improve it or ignore it.
    """

    problem = prepared.problem
    if problem.active_shipments:
        return frozenset()
    constraints = problem.constraints
    current_system = constraints.start_system_id
    current_time = 0
    locked_collateral = 0
    chosen: set[int] = set()
    ranked = sorted(
        prepared.scores,
        key=lambda score: (
            score.reward_per_hour_isk,
            score.contract.contract.reward_units,
            -score.contract.contract.contract_id,
        ),
        reverse=True,
    )
    service = constraints.travel.service_seconds
    jump_seconds = constraints.travel.seconds_per_jump
    required_systems = set(constraints.required_system_ids)
    required_systems.discard(constraints.start_system_id)
    satisfied_required: set[int] = set()

    def tail_completion(
        system_id: int,
        elapsed: int,
        satisfied: set[int],
    ) -> int | None:
        """Return one deterministic feasible waypoint/terminal tail completion time."""

        current = system_id
        completion = elapsed
        remaining = required_systems - satisfied
        terminal = constraints.terminal_system_id
        if terminal is not None:
            remaining.discard(terminal)
        while remaining:
            reachable = [
                (jumps, candidate)
                for candidate in remaining
                if (jumps := prepared.jump_matrix.get((current, candidate))) is not None
            ]
            if not reachable:
                return None
            jumps, destination = min(reachable)
            completion += jumps * jump_seconds
            current = destination
            remaining.remove(destination)
        if terminal is not None:
            jumps = prepared.jump_matrix.get((current, terminal))
            if jumps is None:
                return None
            completion += jumps * jump_seconds
        return completion

    for score in ranked:
        item = score.contract
        contract = item.contract
        to_pickup = prepared.jump_matrix.get((current_system, item.origin_system_id))
        to_delivery = prepared.jump_matrix.get((item.origin_system_id, item.destination_system_id))
        if to_pickup is None or to_delivery is None:
            continue
        pickup_arrival = current_time + to_pickup * jump_seconds
        delivery_arrival = pickup_arrival + service + to_delivery * jump_seconds
        delivery_completion = delivery_arrival + service
        if delivery_completion > constraints.horizon_seconds:
            continue
        newly_satisfied = satisfied_required | (
            {item.origin_system_id, item.destination_system_id} & required_systems
        )
        tail = tail_completion(
            item.destination_system_id,
            delivery_completion,
            newly_satisfied,
        )
        if tail is None or tail > constraints.horizon_seconds:
            continue
        if constraints.collateral_mode is CollateralMode.LOCKED:
            if locked_collateral + contract.collateral_units > constraints.collateral_budget_units:
                continue
            if delivery_completion > contract.days_to_complete * 86_400:
                continue
        else:
            if pickup_arrival > _listing_last_second(prepared, contract.contract_id):
                continue
            if delivery_completion > pickup_arrival + contract.days_to_complete * 86_400:
                continue
        chosen.add(contract.contract_id)
        if constraints.collateral_mode is CollateralMode.LOCKED:
            locked_collateral += contract.collateral_units
        current_time = delivery_completion
        current_system = item.destination_system_id
        satisfied_required = newly_satisfied
    return frozenset(chosen)


def _build_model(prepared: PreparedProblem) -> _ModelArtifacts:
    problem = prepared.problem
    constraints = problem.constraints
    optional_ids = {item.contract.contract_id for item in problem.contracts}
    active_ids = {item.contract.contract.contract_id for item in problem.active_shipments}
    optional_by_id = {item.contract.contract_id: item for item in problem.contracts}
    active_by_id = {item.contract.contract.contract_id: item for item in problem.active_shipments}
    overlap = optional_ids & active_ids
    if overlap:
        raise ValueError(f"contracts cannot be both optional and active: {sorted(overlap)}")

    terminal_system_id = constraints.terminal_system_id
    events: list[_Event] = [
        _Event(0, _START, None, None, constraints.start_system_id, None),
        _Event(1, _END, None, None, terminal_system_id, None),
    ]
    pickup_node: dict[int, int] = {}
    delivery_node: dict[int, int] = {}
    for item in problem.contracts:
        contract = item.contract
        pickup = len(events)
        events.append(
            _Event(
                pickup,
                f"pickup:{contract.contract_id}",
                ActionKind.PICKUP,
                contract.contract_id,
                item.origin_system_id,
                contract.origin_location_id,
                cargo_delta=contract.volume_units,
                collateral_delta=contract.collateral_units,
                parcel_delta=1,
                optional=True,
            )
        )
        delivery = len(events)
        events.append(
            _Event(
                delivery,
                f"delivery:{contract.contract_id}",
                ActionKind.DELIVERY,
                contract.contract_id,
                item.destination_system_id,
                contract.destination_location_id,
                cargo_delta=-contract.volume_units,
                collateral_delta=-contract.collateral_units,
                parcel_delta=-1,
                optional=True,
            )
        )
        pickup_node[contract.contract_id] = pickup
        delivery_node[contract.contract_id] = delivery

    active_pickup_node: dict[int, int] = {}
    active_delivery_node: dict[int, int] = {}
    for shipment in problem.active_shipments:
        item = shipment.contract
        contract = item.contract
        if not shipment.picked:
            pickup = len(events)
            events.append(
                _Event(
                    pickup,
                    f"committed-pickup:{contract.contract_id}",
                    ActionKind.PICKUP,
                    contract.contract_id,
                    item.origin_system_id,
                    contract.origin_location_id,
                    cargo_delta=contract.volume_units,
                    # Collateral was already paid when the contract was accepted.
                    collateral_delta=0,
                    parcel_delta=1,
                    optional=False,
                )
            )
            active_pickup_node[contract.contract_id] = pickup
        node = len(events)
        events.append(
            _Event(
                node,
                f"active-delivery:{contract.contract_id}",
                ActionKind.DELIVERY,
                contract.contract_id,
                item.destination_system_id,
                contract.destination_location_id,
                cargo_delta=-contract.volume_units,
                collateral_delta=-contract.collateral_units,
                parcel_delta=-1,
                optional=False,
            )
        )
        active_delivery_node[contract.contract_id] = node

    waypoint_nodes: list[int] = []
    already_satisfied = {constraints.start_system_id}
    if terminal_system_id is not None:
        already_satisfied.add(terminal_system_id)
    for system_id in sorted(constraints.required_system_ids - already_satisfied):
        node = len(events)
        events.append(
            _Event(
                node,
                f"waypoint:{system_id}",
                None,
                None,
                system_id,
                None,
                optional=False,
            )
        )
        waypoint_nodes.append(node)

    model = cp_model.CpModel()
    selected = {
        item.contract.contract_id: model.new_bool_var(f"select_{item.contract.contract_id}")
        for item in problem.contracts
    }
    route_arcs: dict[tuple[int, int], cp_model.IntVar] = {}
    circuit_arcs: list[tuple[int, int, cp_model.IntVar]] = []

    wrap = model.new_bool_var("end_to_start")
    model.add(wrap == 1)
    route_arcs[(1, 0)] = wrap
    circuit_arcs.append((1, 0, wrap))

    for event in events[2:]:
        if event.optional:
            assert event.contract_id is not None
            skip = model.new_bool_var(f"skip_{event.label}")
            model.add(skip + selected[event.contract_id] == 1)
            circuit_arcs.append((event.node, event.node, skip))
        else:
            # A false self-loop registers the mandatory node with AddCircuit even when every real
            # incoming/outgoing arc is eliminated as impossible. The model then proves infeasible
            # instead of accidentally omitting an unreachable commitment.
            forbidden_skip = model.new_bool_var(f"forbid_skip_{event.label}")
            model.add(forbidden_skip == 0)
            circuit_arcs.append((event.node, event.node, forbidden_skip))

    def travel_seconds(from_event: _Event, to_event: _Event) -> int | None:
        if to_event.node == 1 and to_event.system_id is None:
            return 0
        if from_event.system_id is None or to_event.system_id is None:
            return None
        jumps = prepared.jump_matrix.get((from_event.system_id, to_event.system_id))
        if jumps is None:
            return None
        return jumps * constraints.travel.seconds_per_jump

    horizon = constraints.horizon_seconds
    service = constraints.travel.service_seconds
    jump_seconds = constraints.travel.seconds_per_jump

    required_pickup_for_delivery = {
        delivery_node[contract_id]: pickup_node[contract_id] for contract_id in pickup_node
    }
    required_pickup_for_delivery.update(
        {
            active_delivery_node[contract_id]: active_pickup_node[contract_id]
            for contract_id in active_pickup_node
        }
    )
    delivery_for_required_pickup = {
        pickup: delivery for delivery, pickup in required_pickup_for_delivery.items()
    }

    def direct_travel_seconds(source: int, destination: int) -> int | None:
        jumps = prepared.jump_matrix.get((source, destination))
        return None if jumps is None else jumps * jump_seconds

    def earliest_arrival(event: _Event) -> int:
        if event.node == 0 or event.system_id is None:
            return 0
        direct = direct_travel_seconds(constraints.start_system_id, event.system_id)
        if (
            event.action is not ActionKind.DELIVERY
            or event.node not in required_pickup_for_delivery
        ):
            return horizon + 1 if direct is None else direct

        # An unpicked contract cannot reach its delivery before first reaching and servicing its
        # pickup.  This is a stronger lower bound than start->delivery and remains exact/safe even
        # when the eventual route interleaves other contracts.
        pickup = events[required_pickup_for_delivery[event.node]]
        assert pickup.system_id is not None
        to_pickup = direct_travel_seconds(constraints.start_system_id, pickup.system_id)
        to_delivery = direct_travel_seconds(pickup.system_id, event.system_id)
        if to_pickup is None or to_delivery is None:
            return horizon + 1
        return to_pickup + service + to_delivery

    def latest_arrival(event: _Event) -> int:
        if event.action is None:
            return horizon
        latest = horizon - service
        assert event.contract_id is not None
        optional = optional_by_id.get(event.contract_id)
        if optional is not None:
            if event.action is ActionKind.PICKUP:
                delivery_travel = direct_travel_seconds(
                    optional.origin_system_id,
                    optional.destination_system_id,
                )
                if delivery_travel is not None:
                    latest = min(latest, horizon - 2 * service - delivery_travel)
                    if constraints.collateral_mode is CollateralMode.LOCKED:
                        latest = min(
                            latest,
                            optional.contract.days_to_complete * 86_400
                            - 2 * service
                            - delivery_travel,
                        )
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    latest = min(latest, _listing_last_second(prepared, event.contract_id))
            if (
                event.action is ActionKind.DELIVERY
                and constraints.collateral_mode is CollateralMode.LOCKED
            ):
                latest = min(latest, optional.contract.days_to_complete * 86_400 - service)
            return latest
        active = active_by_id[event.contract_id]
        deadline = int((active.deadline - constraints.snapshot_time).total_seconds())
        if event.action is ActionKind.DELIVERY:
            return min(latest, deadline - service)
        delivery_travel = direct_travel_seconds(
            active.contract.origin_system_id,
            active.contract.destination_system_id,
        )
        if delivery_travel is not None:
            latest = min(
                latest,
                horizon - 2 * service - delivery_travel,
                deadline - 2 * service - delivery_travel,
            )
        return latest

    for from_event in events:
        if from_event.node == 1:
            continue
        for to_event in events:
            if to_event.node == 0 or to_event.node == from_event.node:
                continue
            if (
                (from_event.node == 0 and to_event.node in required_pickup_for_delivery)
                or (from_event.node in delivery_for_required_pickup and to_event.node == 1)
                or required_pickup_for_delivery.get(from_event.node) == to_event.node
            ):
                # These arcs violate the already-required pickup-before-delivery order: delivery
                # cannot be first, pickup cannot be last, and a delivery cannot point back to its
                # own pickup. Omitting them removes no feasible circuit.
                continue
            travel = travel_seconds(from_event, to_event)
            if travel is None:
                continue
            from_service = service if from_event.action is not None else 0
            if earliest_arrival(from_event) + from_service + travel > latest_arrival(to_event):
                # Even the impossible-to-beat direct lower bound cannot reach the destination's
                # hard time window. Omitting this arc is therefore proof-preserving.
                continue
            literal = model.new_bool_var(f"arc_{from_event.node}_{to_event.node}")
            route_arcs[(from_event.node, to_event.node)] = literal
            circuit_arcs.append((from_event.node, to_event.node, literal))
    model.add_circuit(circuit_arcs)

    arrival = [model.new_int_var(0, horizon, f"arrival_{event.node}") for event in events]
    order = [model.new_int_var(0, len(events), f"order_{event.node}") for event in events]
    model.add(arrival[0] == 0)
    model.add(order[0] == 0)

    # Make route-implied event windows explicit so presolve/propagation need not rediscover them
    # through the circuit. Optional bounds are enforced only when that contract is selected.
    for item in problem.contracts:
        contract_id = item.contract.contract_id
        selection = selected[contract_id]
        for node in (pickup_node[contract_id], delivery_node[contract_id]):
            model.add(arrival[node] >= earliest_arrival(events[node])).only_enforce_if(selection)
            model.add(arrival[node] <= latest_arrival(events[node])).only_enforce_if(selection)
    for node in (
        *active_pickup_node.values(),
        *active_delivery_node.values(),
        *waypoint_nodes,
    ):
        model.add(arrival[node] >= earliest_arrival(events[node]))
        model.add(arrival[node] <= latest_arrival(events[node]))

    cargo_capacity = constraints.cargo_capacity_units
    initial_cargo = sum(
        item.contract.contract.volume_units for item in problem.active_shipments if item.picked
    )
    cargo = [model.new_int_var(0, cargo_capacity, f"cargo_{event.node}") for event in events]
    model.add(cargo[0] == initial_cargo)
    model.add(cargo[1] == 0)

    parcel_count: list[cp_model.IntVar] | None = None
    if constraints.max_simultaneous_contracts is not None:
        parcel_limit = constraints.max_simultaneous_contracts
        initial_parcels = sum(1 for item in problem.active_shipments if item.picked)
        parcel_count = [
            model.new_int_var(0, parcel_limit, f"parcels_{event.node}") for event in events
        ]
        model.add(parcel_count[0] == initial_parcels)
        model.add(parcel_count[1] == 0)

    rolling_collateral: list[cp_model.IntVar] | None = None
    initial_collateral = sum(
        item.contract.contract.collateral_units for item in problem.active_shipments
    )
    if constraints.collateral_mode is CollateralMode.ROLLING:
        rolling_collateral = [
            model.new_int_var(
                0,
                constraints.collateral_budget_units,
                f"collateral_{event.node}",
            )
            for event in events
        ]
        model.add(rolling_collateral[0] == initial_collateral)
        model.add(rolling_collateral[1] == 0)
    else:
        model.add(
            initial_collateral
            + sum(
                item.contract.collateral_units * selected[item.contract.contract_id]
                for item in problem.contracts
            )
            <= constraints.collateral_budget_units
        )

    mandatory_actions = sum(1 if shipment.picked else 2 for shipment in problem.active_shipments)
    if service > 0:
        # Every selected optional contract contributes exactly two serviced actions. This simple
        # route-independent inequality is redundant with exact time propagation but materially
        # strengthens the relaxation on dense candidate sets.
        model.add(service * (mandatory_actions + 2 * sum(selected.values())) <= horizon)

    route_travel_terms: list[cp_model.LinearExpr] = []
    for (from_node, to_node), literal in route_arcs.items():
        if (from_node, to_node) == (1, 0):
            continue
        from_event = events[from_node]
        to_event = events[to_node]
        travel = travel_seconds(from_event, to_event)
        assert travel is not None
        if travel:
            route_travel_terms.append(travel * literal)
        from_service = constraints.travel.service_seconds if from_event.action is not None else 0
        model.add(arrival[to_node] == arrival[from_node] + from_service + travel).only_enforce_if(
            literal
        )
        model.add(order[to_node] == order[from_node] + 1).only_enforce_if(literal)
        model.add(cargo[to_node] == cargo[from_node] + to_event.cargo_delta).only_enforce_if(
            literal
        )
        if parcel_count is not None:
            model.add(
                parcel_count[to_node] == parcel_count[from_node] + to_event.parcel_delta
            ).only_enforce_if(literal)
        if rolling_collateral is not None:
            model.add(
                rolling_collateral[to_node]
                == rolling_collateral[from_node] + to_event.collateral_delta
            ).only_enforce_if(literal)

    # This equality is implied by the event-by-event arrival propagation for every integral route,
    # but exposing the complete travel budget in one linear row gives CP-SAT's relaxation a direct
    # link between selecting reward, paying service time and paying the chosen arc distances.
    model.add(
        arrival[1]
        == sum(route_travel_terms) + service * (mandatory_actions + 2 * sum(selected.values()))
    )

    for item in problem.contracts:
        contract = item.contract
        selection = selected[contract.contract_id]
        pickup = pickup_node[contract.contract_id]
        delivery = delivery_node[contract.contract_id]
        model.add(order[delivery] >= order[pickup] + 1).only_enforce_if(selection)
        direct_delivery_travel = direct_travel_seconds(
            item.origin_system_id,
            item.destination_system_id,
        )
        assert direct_delivery_travel is not None
        # Whatever is interleaved between this pickup and delivery cannot beat the metric-closure
        # shortest path. Exposing this implied inequality directly gives CP-SAT substantially
        # stronger time propagation than relying on a chain of conditional route arcs alone.
        model.add(
            arrival[delivery] >= arrival[pickup] + service + direct_delivery_travel
        ).only_enforce_if(selection)
        if constraints.collateral_mode is CollateralMode.LOCKED:
            model.add(
                arrival[delivery] + service <= contract.days_to_complete * 86_400
            ).only_enforce_if(selection)
        else:
            # Acceptance at the exact expiry timestamp is already too late. Arrival variables are
            # integral seconds, so ceil(delta)-1 is the greatest strictly-before expiry value.
            listing_last_second = _listing_last_second(prepared, contract.contract_id)
            model.add(arrival[pickup] <= listing_last_second).only_enforce_if(selection)
            model.add(
                arrival[delivery] + service <= arrival[pickup] + contract.days_to_complete * 86_400
            ).only_enforce_if(selection)

    for shipment in problem.active_shipments:
        contract_id = shipment.contract.contract.contract_id
        node = active_delivery_node[contract_id]
        if not shipment.picked:
            pickup = active_pickup_node[contract_id]
            model.add(order[node] >= order[pickup] + 1)
            direct_delivery_travel = direct_travel_seconds(
                shipment.contract.origin_system_id,
                shipment.contract.destination_system_id,
            )
            if direct_delivery_travel is not None:
                model.add(arrival[node] >= arrival[pickup] + service + direct_delivery_travel)
        deadline_seconds = int((shipment.deadline - constraints.snapshot_time).total_seconds())
        model.add(arrival[node] + service <= deadline_seconds)

    active_reward = sum(item.contract.contract.reward_units for item in problem.active_shipments)
    maximum_reward = active_reward + sum(item.contract.reward_units for item in problem.contracts)
    total_reward = model.new_int_var(active_reward, maximum_reward, "total_reward")
    model.add(
        total_reward
        == active_reward
        + sum(
            item.contract.reward_units * selected[item.contract.contract_id]
            for item in problem.contracts
        )
    )
    model.maximize(total_reward)
    hint_ids = _greedy_hint_ids(prepared)
    for contract_id, literal in selected.items():
        model.add_hint(literal, int(contract_id in hint_ids))

    return _ModelArtifacts(
        model=model,
        events=tuple(events),
        start_node=0,
        end_node=1,
        selected=selected,
        route_arcs=route_arcs,
        total_reward=total_reward,
        finish_time=arrival[1],
    )


def _primary_stats(
    solver: cp_model.CpSolver,
    status: cp_model.CpSolverStatus,
    objective_variable: cp_model.IntVar,
) -> _PrimaryStats:
    name = solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _PrimaryStats(
            status=status,
            status_name=name,
            objective_units=None,
            bound_units=None,
            wall_time=solver.wall_time,
            branches=solver.num_branches,
            conflicts=solver.num_conflicts,
        )
    # Read the modeled integer directly. ``objective_value`` is exposed as a double and can lose
    # unit precision above 2**53, while ``value(IntVar)`` preserves the exact CP-SAT integer.
    objective = int(solver.value(objective_variable))
    if status == cp_model.OPTIMAL:
        bound = objective
    else:
        # For a maximization problem CP-SAT's best objective bound is a rigorous upper bound.
        # The Python API exposes that bound as a double. Integers through 2**53-1 are represented
        # exactly; above that, widen by one floating ULP before taking the ceiling so a conversion
        # roundoff can never make the reported integer upper bound spuriously too small.
        raw_bound = solver.best_objective_bound
        if abs(raw_bound) <= 2**53 - 1:
            bound = int(math.ceil(raw_bound))
        else:
            bound = int(math.ceil(math.nextafter(raw_bound, math.inf)))
        bound = max(bound, objective)
    return _PrimaryStats(
        status=status,
        status_name=name,
        objective_units=objective,
        bound_units=bound,
        wall_time=solver.wall_time,
        branches=solver.num_branches,
        conflicts=solver.num_conflicts,
    )


def _extract_visits(
    artifacts: _ModelArtifacts,
    solver: cp_model.CpSolver,
) -> tuple[tuple[PlannedVisit, ...], tuple[int, ...]]:
    selected = tuple(
        sorted(
            contract_id
            for contract_id, literal in artifacts.selected.items()
            if solver.value(literal)
        )
    )
    successor: dict[int, int] = {}
    for (source, destination), literal in artifacts.route_arcs.items():
        if solver.value(literal):
            successor[source] = destination
    visits: list[PlannedVisit] = []
    current = artifacts.start_node
    seen = {current}
    while True:
        next_node = successor.get(current)
        if next_node is None:
            raise RuntimeError(f"solver route has no successor for node {current}")
        if next_node == artifacts.end_node:
            break
        if next_node in seen:
            raise RuntimeError("solver route contains a cycle before the end node")
        seen.add(next_node)
        event = artifacts.events[next_node]
        if event.action is None:
            if event.system_id is None or event.contract_id is not None:
                raise RuntimeError("unexpected anonymous event inside solver route")
            visits.append(PlannedWaypoint(event.system_id))
        else:
            if event.contract_id is None:
                raise RuntimeError("contract action is missing its contract ID")
            visits.append(PlannedAction(event.action, event.contract_id))
        current = next_node
    return tuple(visits), selected


def _dense_selection_cuts(prepared: PreparedProblem) -> SelectionCuts:
    if len(prepared.problem.contracts) < _BOUND_STRENGTHENING_MIN_CONTRACTS:
        return SelectionCuts((), ())
    return build_selection_cuts(prepared)


def _selection_subset(
    prepared: PreparedProblem,
    contract_ids: tuple[int, ...],
) -> PreparedProblem:
    """Keep only one optional contract set while preserving every mandatory route constraint.

    Optional courier feasibility is downward-closed. Removing optional pickup/delivery pairs can
    only remove service time, cargo, collateral and parcel load. The jump matrix is a metric
    closure, so shortcutting deleted actions never lengthens travel. Mandatory waypoints, active
    shipments and the required finish remain in the problem. Therefore, if a reduced set C is
    infeasible, every full-universe selection containing C is infeasible too.
    """

    wanted = frozenset(contract_ids)
    known = {item.contract.contract_id for item in prepared.problem.contracts}
    unknown = tuple(sorted(wanted - known))
    if unknown:
        raise ValueError(f"reduced exact selection contains unknown contract IDs: {unknown}")
    contracts = tuple(
        item for item in prepared.problem.contracts if item.contract.contract_id in wanted
    )
    scores = tuple(
        score
        for score in prepared.scores
        if score.contract.contract.contract_id in wanted
    )
    return PreparedProblem(
        problem=replace(prepared.problem, contracts=contracts),
        jump_matrix=prepared.jump_matrix,
        scores=scores,
    )


def _subproblem_solver(config: SolverConfig, max_time_seconds: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    solver.parameters.num_search_workers = config.num_workers
    solver.parameters.random_seed = config.random_seed
    solver.parameters.log_search_progress = config.log_search_progress
    return solver


def _solve_reduced_exact_oracle(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    contract_ids: tuple[int, ...],
    config: SolverConfig,
    *,
    max_time_seconds: float,
) -> _ExactOracleResult:
    """Test one master selection exactly and return a sufficient infeasibility core when needed."""

    reduced = _selection_subset(prepared, contract_ids)
    artifacts = _build_model(reduced)
    artifacts.model.maximize(0)
    by_literal_index = {literal.index: item for item, literal in artifacts.selected.items()}
    deadline = time.perf_counter() + max_time_seconds
    total_wall_time = 0.0
    total_branches = 0
    total_conflicts = 0

    def solve_with_assumptions(
        assumed_ids: tuple[int, ...],
        seconds: float,
    ) -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus]:
        artifacts.model.clear_assumptions()
        artifacts.model.add_assumptions([artifacts.selected[item] for item in assumed_ids])
        validation_error = artifacts.model.validate()
        if validation_error:
            raise ValueError(f"invalid reduced exact model: {validation_error}")
        solver = _subproblem_solver(config, seconds)
        status = solver.solve(artifacts.model)
        if status == cp_model.MODEL_INVALID:
            raise ValueError("CP-SAT rejected the validated reduced exact model")
        return solver, status

    initial_solver, status = solve_with_assumptions(contract_ids, max_time_seconds)
    total_wall_time += initial_solver.wall_time
    total_branches += initial_solver.num_branches
    total_conflicts += initial_solver.num_conflicts
    status_name = initial_solver.status_name(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        visits, selected = _extract_visits(artifacts, initial_solver)
        expected = tuple(sorted(contract_ids))
        if selected != expected:
            raise RuntimeError(f"reduced exact oracle did not enforce its selection: {selected}")
        simulation = simulate_and_verify(prepared.problem, graph, visits, selected)
        if not simulation.report.valid:
            raise RuntimeError(
                "reduced exact route failed independent full-problem verification: "
                + "; ".join(simulation.report.violations)
            )
        return _ExactOracleResult(
            status=status,
            status_name=status_name,
            selected_contract_ids=selected,
            simulation=simulation,
            infeasible_core_ids=(),
            wall_time=total_wall_time,
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
            wall_time=total_wall_time,
            branches=total_branches,
            conflicts=total_conflicts,
        )

    raw_core = initial_solver.sufficient_assumptions_for_infeasibility()
    unexpected = tuple(index for index in raw_core if index not in by_literal_index)
    if unexpected:
        raise RuntimeError(f"CP-SAT returned unexpected assumption literals: {unexpected}")
    core = tuple(sorted(by_literal_index[index] for index in raw_core))

    # CP-SAT promises a sufficient core, not a minimal one. Deletion checks can make the learned
    # master cut substantially stronger. A literal is removed only after another exact INFEASIBLE
    # result, so an UNKNOWN shrink attempt can never make the proof unsafe.
    for contract_id in tuple(core):
        if contract_id not in core:
            continue
        remaining = deadline - time.perf_counter()
        if remaining <= 0.001:
            break
        trial = tuple(item for item in core if item != contract_id)
        shrink_solver, shrink_status = solve_with_assumptions(trial, remaining)
        total_wall_time += shrink_solver.wall_time
        total_branches += shrink_solver.num_branches
        total_conflicts += shrink_solver.num_conflicts
        if shrink_status != cp_model.INFEASIBLE:
            continue
        shrink_raw_core = shrink_solver.sufficient_assumptions_for_infeasibility()
        shrink_unexpected = tuple(
            index for index in shrink_raw_core if index not in by_literal_index
        )
        if shrink_unexpected:
            raise RuntimeError(
                f"CP-SAT returned unexpected shrink-core literals: {shrink_unexpected}"
            )
        core = tuple(sorted(by_literal_index[index] for index in shrink_raw_core))

    return _ExactOracleResult(
        status=status,
        status_name=status_name,
        selected_contract_ids=(),
        simulation=None,
        infeasible_core_ids=core,
        wall_time=total_wall_time,
        branches=total_branches,
        conflicts=total_conflicts,
    )


def _refine_fixed_selection_finish_time(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    contract_ids: tuple[int, ...],
    config: SolverConfig,
) -> _ExactOracleResult:
    """Optionally minimize finish time after the decomposition has already proved max reward."""

    reduced = _selection_subset(prepared, contract_ids)
    artifacts = _build_model(reduced)
    for contract_id in contract_ids:
        artifacts.model.add(artifacts.selected[contract_id] == 1)
    artifacts.model.minimize(artifacts.finish_time)
    validation_error = artifacts.model.validate()
    if validation_error:
        raise ValueError(f"invalid fixed-selection refinement model: {validation_error}")
    solver = _subproblem_solver(config, config.secondary_time_seconds)
    status = solver.solve(artifacts.model)
    if status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT rejected the validated fixed-selection refinement model")
    simulation: SimulationResult | None = None
    selected: tuple[int, ...] = ()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        visits, selected = _extract_visits(artifacts, solver)
        expected = tuple(sorted(contract_ids))
        if selected != expected:
            raise RuntimeError(f"fixed-selection refinement changed its contract set: {selected}")
        simulation = simulate_and_verify(prepared.problem, graph, visits, selected)
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
        selected_contract_ids=selected,
        simulation=simulation,
        infeasible_core_ids=(),
        wall_time=solver.wall_time,
        branches=solver.num_branches,
        conflicts=solver.num_conflicts,
    )


def _run_dense_decomposition(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    config: SolverConfig,
) -> _DecompositionOutcome:
    """Run the endpoint master and exact reduced subproblems before the monolithic fallback."""

    selection_cuts = _dense_selection_cuts(prepared)
    if len(prepared.problem.contracts) < _BOUND_STRENGTHENING_MIN_CONTRACTS:
        return _DecompositionOutcome(
            None, selection_cuts, None, (), False, None, 0, (), 0.0, 0, 0
        )
    if config.relaxation_time_seconds <= 0:
        return _DecompositionOutcome(
            None, selection_cuts, None, (), False, "disabled", 0, (), 0.0, 0, 0
        )

    master = build_system_relaxation_master(prepared, selection_cuts=selection_cuts)
    if config.decomposition_time_seconds == 0:
        bound = solve_system_relaxation_master(
            master,
            max_time_seconds=config.relaxation_time_seconds,
            random_seed=config.random_seed,
        )
        return _DecompositionOutcome(
            bound, selection_cuts, None, (), False, "bound_only", 0, (), 0.0, 0, 0
        )

    deadline = time.perf_counter() + config.decomposition_time_seconds
    learned: list[tuple[int, ...]] = []
    relaxation: SystemRelaxationBound | None = None
    master_wall_time = 0.0
    master_branches = 0
    master_conflicts = 0
    subproblem_wall_time = 0.0
    subproblem_branches = 0
    subproblem_conflicts = 0
    iterations = 0
    status_name = "budget_exhausted"
    proven_infeasible = False
    simulation: SimulationResult | None = None
    selected_contract_ids: tuple[int, ...] = ()

    for _ in range(config.decomposition_max_iterations):
        remaining = deadline - time.perf_counter()
        if remaining <= 0.001:
            break
        iterations += 1
        current = solve_system_relaxation_master(
            master,
            max_time_seconds=min(config.relaxation_time_seconds, remaining),
            random_seed=config.random_seed,
        )
        master_wall_time += current.wall_time_seconds
        master_branches += current.branches
        master_conflicts += current.conflicts
        relaxation = current
        if current.status_name == "INFEASIBLE":
            proven_infeasible = True
            status_name = "master_infeasible"
            break
        if current.status_name != "OPTIMAL":
            status_name = f"master_{current.status_name.lower()}"
            break

        remaining = deadline - time.perf_counter()
        if remaining <= 0.001:
            status_name = "budget_exhausted"
            break
        oracle = _solve_reduced_exact_oracle(
            prepared,
            graph,
            current.selected_contract_ids,
            config,
            max_time_seconds=min(config.decomposition_subproblem_time_seconds, remaining),
        )
        subproblem_wall_time += oracle.wall_time
        subproblem_branches += oracle.branches
        subproblem_conflicts += oracle.conflicts
        if oracle.status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if oracle.simulation is None or current.objective_units is None:
                raise RuntimeError("feasible reduced exact oracle is missing its verified solution")
            if oracle.simulation.total_reward_units != current.objective_units:
                raise RuntimeError(
                    "master objective disagrees with the verified reduced exact route"
                )
            if current.upper_bound_units != oracle.simulation.total_reward_units:
                raise RuntimeError("optimal system master returned an inconsistent objective bound")
            simulation = oracle.simulation
            selected_contract_ids = oracle.selected_contract_ids
            status_name = "bound_matched"
            if config.minimize_finish_time_after_proof:
                refined = _refine_fixed_selection_finish_time(
                    prepared,
                    graph,
                    selected_contract_ids,
                    config,
                )
                subproblem_wall_time += refined.wall_time
                subproblem_branches += refined.branches
                subproblem_conflicts += refined.conflicts
                if (
                    refined.simulation is not None
                    and refined.simulation.finish_seconds < simulation.finish_seconds
                ):
                    simulation = refined.simulation
            break
        if oracle.status != cp_model.INFEASIBLE:
            status_name = f"oracle_{oracle.status_name.lower()}"
            break
        core = oracle.infeasible_core_ids
        if not core:
            proven_infeasible = True
            status_name = "exact_base_infeasible"
            break
        if core in learned:
            raise RuntimeError("logic-based decomposition produced a duplicate infeasibility core")
        add_proven_infeasible_selection_cut(master, core)
        learned.append(core)
        status_name = "core_learned"
    else:
        status_name = "iteration_limit"

    if relaxation is not None:
        relaxation = replace(
            relaxation,
            wall_time_seconds=master_wall_time,
            branches=master_branches,
            conflicts=master_conflicts,
        )
    return _DecompositionOutcome(
        relaxation=relaxation,
        selection_cuts=selection_cuts,
        simulation=simulation,
        selected_contract_ids=selected_contract_ids,
        proven_infeasible=proven_infeasible,
        status_name=status_name,
        iterations=iterations,
        learned_cuts=tuple(learned),
        subproblem_wall_time=subproblem_wall_time,
        subproblem_branches=subproblem_branches,
        subproblem_conflicts=subproblem_conflicts,
    )


def solve_exact(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    *,
    config: SolverConfig | None = None,
) -> SolveResult:
    """Solve the exact prize-collecting pickup/delivery model and report proof metadata."""

    settings = config or SolverConfig()
    decomposition = _run_dense_decomposition(prepared, graph, settings)
    relaxation = decomposition.relaxation
    selection_cuts = decomposition.selection_cuts
    problem_hash = canonical_problem_sha256(prepared.problem, prepared.jump_matrix)
    relaxation_wall_time = relaxation.wall_time_seconds if relaxation is not None else 0.0
    relaxation_branches = relaxation.branches if relaxation is not None else 0
    relaxation_conflicts = relaxation.conflicts if relaxation is not None else 0
    relaxation_status = relaxation.status_name if relaxation is not None else None
    relaxation_bound = relaxation.upper_bound_units if relaxation is not None else None
    relaxation_systems = relaxation.routed_systems if relaxation is not None else 0
    prepass_wall_time = relaxation_wall_time + decomposition.subproblem_wall_time
    prepass_branches = relaxation_branches + decomposition.subproblem_branches
    prepass_conflicts = relaxation_conflicts + decomposition.subproblem_conflicts

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
            problem_sha256=problem_hash,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=prepass_wall_time,
            branches=prepass_branches,
            conflicts=prepass_conflicts,
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
            decomposition_iterations=decomposition.iterations,
            decomposition_learned_cuts=len(decomposition.learned_cuts),
            decomposition_subproblem_wall_time_seconds=decomposition.subproblem_wall_time,
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
            problem_sha256=problem_hash,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=prepass_wall_time,
            branches=prepass_branches,
            conflicts=prepass_conflicts,
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
            decomposition_iterations=decomposition.iterations,
            decomposition_learned_cuts=len(decomposition.learned_cuts),
            decomposition_subproblem_wall_time_seconds=decomposition.subproblem_wall_time,
            decomposition_proof_closed=True,
        )
        return SolveResult((), (), 0, 0, certificate)

    artifacts = _build_model(prepared)
    _apply_bound_strengthening(
        artifacts,
        relaxation,
        selection_cuts,
        decomposition.learned_cuts,
    )
    validation_error = artifacts.model.validate()
    if validation_error:
        raise ValueError(f"invalid or numerically unsafe CP-SAT model: {validation_error}")
    primary_solver = _solver(settings)
    primary_status = primary_solver.solve(artifacts.model)
    primary = _primary_stats(primary_solver, primary_status, artifacts.total_reward)

    if primary_status == cp_model.INFEASIBLE:
        certificate = OptimalityCertificate(
            status=ProofStatus.PROVEN_INFEASIBLE,
            solver_status=primary.status_name,
            objective_units=None,
            best_bound_units=None,
            absolute_gap_units=None,
            relative_gap=None,
            problem_sha256=problem_hash,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=prepass_wall_time + primary.wall_time,
            branches=prepass_branches + primary.branches,
            conflicts=prepass_conflicts + primary.conflicts,
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
            decomposition_iterations=decomposition.iterations,
            decomposition_learned_cuts=len(decomposition.learned_cuts),
            decomposition_subproblem_wall_time_seconds=decomposition.subproblem_wall_time,
            decomposition_proof_closed=False,
        )
        return SolveResult((), (), 0, 0, certificate)
    if primary_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        certificate = OptimalityCertificate(
            status=ProofStatus.UNKNOWN,
            solver_status=primary.status_name,
            objective_units=None,
            best_bound_units=relaxation_bound,
            absolute_gap_units=None,
            relative_gap=None,
            problem_sha256=problem_hash,
            solver_name="OR-Tools CP-SAT",
            solver_version=package_version("ortools"),
            wall_time_seconds=prepass_wall_time + primary.wall_time,
            branches=prepass_branches + primary.branches,
            conflicts=prepass_conflicts + primary.conflicts,
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
            decomposition_iterations=decomposition.iterations,
            decomposition_learned_cuts=len(decomposition.learned_cuts),
            decomposition_subproblem_wall_time_seconds=decomposition.subproblem_wall_time,
            decomposition_proof_closed=False,
        )
        return SolveResult((), (), 0, 0, certificate)

    solution_solver = primary_solver
    total_wall_time = prepass_wall_time + primary.wall_time
    total_branches = prepass_branches + primary.branches
    total_conflicts = prepass_conflicts + primary.conflicts
    if primary_status == cp_model.OPTIMAL and settings.minimize_finish_time_after_proof:
        assert primary.objective_units is not None
        artifacts.model.add(artifacts.total_reward == primary.objective_units)
        artifacts.model.minimize(artifacts.finish_time)
        secondary_solver = _solver(settings, secondary=True)
        secondary_status = secondary_solver.solve(artifacts.model)
        if secondary_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            solution_solver = secondary_solver
            total_wall_time += secondary_solver.wall_time
            total_branches += secondary_solver.num_branches
            total_conflicts += secondary_solver.num_conflicts

    visits, selected = _extract_visits(artifacts, solution_solver)
    simulation = simulate_and_verify(prepared.problem, graph, visits, selected)
    if not simulation.report.valid:
        raise RuntimeError(
            "solver returned a route that failed independent verification: "
            + "; ".join(simulation.report.violations)
        )
    if primary.objective_units is None or simulation.total_reward_units != primary.objective_units:
        raise RuntimeError("independent simulation reward does not match solver objective")

    reference_verified = False
    if (
        primary_status == cp_model.OPTIMAL
        and prepared.problem.constraints.collateral_mode is CollateralMode.LOCKED
        and not prepared.problem.active_shipments
        and not prepared.problem.constraints.required_system_ids
        and len(prepared.problem.contracts) <= settings.independent_reference_limit
    ):
        reference = solve_reference(
            prepared,
            contract_limit=settings.independent_reference_limit,
        )
        if reference.objective_units != primary.objective_units:
            raise RuntimeError(
                "CP-SAT optimum disagrees with independent exhaustive reference solver: "
                f"{primary.objective_units} != {reference.objective_units}"
            )
        reference_verified = True

    assert primary.bound_units is not None
    absolute_gap = max(0, primary.bound_units - primary.objective_units)
    relative_gap = absolute_gap / max(1, abs(primary.objective_units))
    proof_status = (
        ProofStatus.PROVEN_OPTIMAL
        if primary_status == cp_model.OPTIMAL
        else ProofStatus.FEASIBLE_NOT_PROVEN
    )
    certificate = OptimalityCertificate(
        status=proof_status,
        solver_status=primary.status_name,
        objective_units=primary.objective_units,
        best_bound_units=primary.bound_units,
        absolute_gap_units=absolute_gap,
        relative_gap=relative_gap,
        problem_sha256=problem_hash,
        solver_name="OR-Tools CP-SAT",
        solver_version=package_version("ortools"),
        wall_time_seconds=total_wall_time,
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
        decomposition_iterations=decomposition.iterations,
        decomposition_learned_cuts=len(decomposition.learned_cuts),
        decomposition_subproblem_wall_time_seconds=decomposition.subproblem_wall_time,
        decomposition_proof_closed=False,
    )
    return SolveResult(
        selected_contract_ids=selected,
        route=simulation.steps,
        total_reward_units=simulation.total_reward_units,
        finish_seconds=simulation.finish_seconds,
        certificate=certificate,
        travel_legs=simulation.travel_legs,
    )
