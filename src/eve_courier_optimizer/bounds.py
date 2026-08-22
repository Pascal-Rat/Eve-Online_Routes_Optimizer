"""Proof-preserving upper-bound strengthening for dense courier instances.

The exact pickup/delivery model is deliberately expressive.  That expressiveness can make its
linear relaxation weak because time and resource state are carried through conditional event arcs.
This module derives simpler necessary conditions that every exact route must satisfy.  They can
therefore tighten proof search without deleting a feasible courier solution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

from ortools.sat.python import cp_model

from .domain import CollateralMode, RoutableContract
from .planning import PreparedProblem


@dataclass(frozen=True, slots=True)
class SystemRelaxationBound:
    """A rigorous reward ceiling from the endpoint-system route relaxation."""

    status_name: str
    upper_bound_units: int | None
    objective_units: int | None
    wall_time_seconds: float
    branches: int
    conflicts: int
    routed_systems: int
    selected_contract_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class SystemRelaxationMaster:
    """Reusable endpoint-system master model for proof-guided decomposition."""

    model: cp_model.CpModel
    contract_is_selected: dict[int, cp_model.IntVar]
    total_reward_units: cp_model.IntVar
    routed_systems: int


@dataclass(frozen=True, slots=True)
class SelectionCuts:
    """Redundant incompatibility constraints expressed only over contract selection."""

    pairs: tuple[tuple[int, int], ...]
    cliques: tuple[tuple[int, ...], ...]


def _integer_upper_bound(raw_bound: float) -> int:
    """Conservatively convert CP-SAT's double objective bound to an integer ceiling."""

    if abs(raw_bound) <= 2**53 - 1:
        return int(math.ceil(raw_bound))
    return int(math.ceil(math.nextafter(raw_bound, math.inf)))


def _active_reward(prepared: PreparedProblem) -> int:
    return sum(
        shipment.contract.contract.reward_units for shipment in prepared.problem.active_shipments
    )


def _active_collateral(prepared: PreparedProblem) -> int:
    return sum(
        shipment.contract.contract.collateral_units
        for shipment in prepared.problem.active_shipments
    )


def _mandatory_action_count(prepared: PreparedProblem) -> int:
    return sum(1 if shipment.picked else 2 for shipment in prepared.problem.active_shipments)


def _collect_relaxation_system_ids(
    prepared: PreparedProblem,
) -> tuple[set[int], set[int]]:
    """Return all endpoint systems and the subset that a real route must visit."""

    problem = prepared.problem
    constraints = problem.constraints
    candidate_system_ids = set(constraints.required_system_ids)
    mandatory_system_ids = set(constraints.required_system_ids)
    for contract in problem.contracts:
        candidate_system_ids.add(contract.origin_system_id)
        candidate_system_ids.add(contract.destination_system_id)
    for shipment in problem.active_shipments:
        if not shipment.picked:
            candidate_system_ids.add(shipment.contract.origin_system_id)
            mandatory_system_ids.add(shipment.contract.origin_system_id)
        candidate_system_ids.add(shipment.contract.destination_system_id)
        mandatory_system_ids.add(shipment.contract.destination_system_id)
    candidate_system_ids.add(constraints.start_system_id)
    if constraints.terminal_system_id is not None:
        candidate_system_ids.add(constraints.terminal_system_id)
        mandatory_system_ids.add(constraints.terminal_system_id)
    return candidate_system_ids, mandatory_system_ids


def build_system_relaxation_master(
    prepared: PreparedProblem,
    *,
    selection_cuts: SelectionCuts | None = None,
) -> SystemRelaxationMaster:
    """Build the endpoint-system route relaxation while retaining its selection literals.

    Every exact courier route maps to this model after repeated visits to the same endpoint system
    are shortcut through the metric closure.  The relaxation retains endpoint visitation, total
    service time, the route horizon and locked collateral, while intentionally dropping action
    order, cargo, parcel state, rolling collateral state and individual deadlines.  Its optimum is
    therefore never below the exact courier optimum.
    """

    problem = prepared.problem
    constraints = problem.constraints
    candidate_system_ids, mandatory_system_ids = _collect_relaxation_system_ids(prepared)
    start_system_id = constraints.start_system_id
    terminal_system_id = constraints.terminal_system_id
    intermediate_system_ids = tuple(
        sorted(
            candidate_system_ids
            - {start_system_id}
            - ({terminal_system_id} if terminal_system_id else set())
        )
    )
    system_id_by_node_id: dict[int, int | None] = {
        0: start_system_id,
        1: terminal_system_id,
    }
    node_id_by_system_id: dict[int, int] = {}
    for node_id, system_id in enumerate(intermediate_system_ids, start=2):
        system_id_by_node_id[node_id] = system_id
        node_id_by_system_id[system_id] = node_id

    model = cp_model.CpModel()
    contract_is_selected = {
        contract.contract.contract_id: model.new_bool_var(
            f"relax_select_{contract.contract.contract_id}"
        )
        for contract in problem.contracts
    }
    effective_selection_cuts = (
        selection_cuts if selection_cuts is not None else build_selection_cuts(prepared)
    )
    contract_pairs_already_covered_by_cliques = {
        tuple(sorted(pair))
        for clique in effective_selection_cuts.cliques
        for pair in combinations(clique, 2)
    }
    for mutually_exclusive_contract_ids in effective_selection_cuts.cliques:
        model.add(
            sum(
                contract_is_selected[contract_id] for contract_id in mutually_exclusive_contract_ids
            )
            <= 1
        )
    for first_contract_id, second_contract_id in effective_selection_cuts.pairs:
        if (
            first_contract_id,
            second_contract_id,
        ) not in contract_pairs_already_covered_by_cliques:
            model.add(
                contract_is_selected[first_contract_id] + contract_is_selected[second_contract_id]
                <= 1
            )
    system_is_visited = {
        system_id: model.new_bool_var(f"relax_visit_{system_id}")
        for system_id in intermediate_system_ids
    }

    selection_literals_incident_to_system: dict[int, list[cp_model.IntVar]] = {
        system_id: [] for system_id in intermediate_system_ids
    }
    for contract in problem.contracts:
        is_contract_selected = contract_is_selected[contract.contract.contract_id]
        for system_id in {
            contract.origin_system_id,
            contract.destination_system_id,
        }:
            if system_id in system_is_visited:
                model.add(is_contract_selected <= system_is_visited[system_id])
                selection_literals_incident_to_system[system_id].append(is_contract_selected)
    for system_id, is_system_visited in system_is_visited.items():
        if system_id in mandatory_system_ids:
            model.add(is_system_visited == 1)
        elif selection_literals_incident_to_system[system_id]:
            model.add(is_system_visited <= sum(selection_literals_incident_to_system[system_id]))
        else:
            model.add(is_system_visited == 0)

    circuit_arc_definitions: list[tuple[int, int, cp_model.IntVar]] = []
    end_to_start_arc_is_used = model.new_bool_var("relax_end_to_start")
    model.add(end_to_start_arc_is_used == 1)
    circuit_arc_definitions.append((1, 0, end_to_start_arc_is_used))
    for system_id, node_id in node_id_by_system_id.items():
        system_is_skipped = model.new_bool_var(f"relax_skip_{system_id}")
        model.add(system_is_skipped + system_is_visited[system_id] == 1)
        circuit_arc_definitions.append((node_id, node_id, system_is_skipped))

    travel_time_terms: list[cp_model.LinearExpr] = []
    node_ids = tuple(sorted(system_id_by_node_id))
    for source_node_id in node_ids:
        if source_node_id == 1:
            continue
        source_system_id = system_id_by_node_id[source_node_id]
        assert source_system_id is not None
        for destination_node_id in node_ids:
            if destination_node_id == 0 or destination_node_id == source_node_id:
                continue
            destination_system_id = system_id_by_node_id[destination_node_id]
            if destination_node_id == 1 and destination_system_id is None:
                jump_count = 0
            elif destination_system_id is None:
                continue
            else:
                possible_jump_count = prepared.jump_matrix.get(
                    (source_system_id, destination_system_id)
                )
                if possible_jump_count is None:
                    continue
                jump_count = possible_jump_count
            is_arc_used = model.new_bool_var(f"relax_arc_{source_node_id}_{destination_node_id}")
            circuit_arc_definitions.append((source_node_id, destination_node_id, is_arc_used))
            travel_time_seconds = jump_count * constraints.travel.seconds_per_jump
            if travel_time_seconds:
                travel_time_terms.append(travel_time_seconds * is_arc_used)
    model.add_circuit(circuit_arc_definitions)

    selected_contract_count = sum(contract_is_selected.values())
    service_time_seconds = constraints.travel.service_seconds * (
        _mandatory_action_count(prepared) + 2 * selected_contract_count
    )
    model.add(sum(travel_time_terms) + service_time_seconds <= constraints.horizon_seconds)

    if constraints.collateral_mode is CollateralMode.LOCKED:
        model.add(
            _active_collateral(prepared)
            + sum(
                contract.contract.collateral_units
                * contract_is_selected[contract.contract.contract_id]
                for contract in problem.contracts
            )
            <= constraints.collateral_budget_units
        )

    committed_reward_units = _active_reward(prepared)
    maximum_reward_units = committed_reward_units + sum(
        contract.contract.reward_units for contract in problem.contracts
    )
    total_reward_units = model.new_int_var(
        committed_reward_units,
        maximum_reward_units,
        "relax_total_reward",
    )
    model.add(
        total_reward_units
        == committed_reward_units
        + sum(
            contract.contract.reward_units * contract_is_selected[contract.contract.contract_id]
            for contract in problem.contracts
        )
    )
    model.maximize(total_reward_units)

    validation_error = model.validate()
    if validation_error:
        raise ValueError(f"invalid system-relaxation model: {validation_error}")
    return SystemRelaxationMaster(
        model=model,
        contract_is_selected=contract_is_selected,
        total_reward_units=total_reward_units,
        routed_systems=len(candidate_system_ids),
    )


def add_proven_infeasible_selection_cut(
    master: SystemRelaxationMaster,
    contract_ids: tuple[int, ...],
) -> None:
    """Forbid one rigorously proven infeasible coexistence set in the master.

    The caller is responsible for proving that every route containing all listed contracts is
    infeasible. The logic-based decomposition does that with positive assumptions in the exact
    pickup/delivery model. This function only encodes the resulting no-good inequality.
    """

    normalized_contract_ids = tuple(sorted(set(contract_ids)))
    if not normalized_contract_ids:
        raise ValueError("an infeasible selection cut must contain at least one contract")
    unknown_contract_ids = tuple(
        contract_id
        for contract_id in normalized_contract_ids
        if contract_id not in master.contract_is_selected
    )
    if unknown_contract_ids:
        raise ValueError(f"selection cut contains unknown contract IDs: {unknown_contract_ids}")
    master.model.add(
        sum(master.contract_is_selected[contract_id] for contract_id in normalized_contract_ids)
        <= len(normalized_contract_ids) - 1
    )


def solve_system_relaxation_master(
    master: SystemRelaxationMaster,
    *,
    max_time_seconds: float,
    random_seed: int = 0,
) -> SystemRelaxationBound:
    """Solve a reusable system master and expose both its ceiling and chosen contracts."""

    if max_time_seconds <= 0:
        raise ValueError("system-relaxation time limit must be positive")
    validation_error = master.model.validate()
    if validation_error:
        raise ValueError(f"invalid system-relaxation model: {validation_error}")
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    # Keep this proof prepass reproducible and avoid portfolio overhead on the smaller bound model.
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = random_seed
    status = solver.solve(master.model)
    status_name = solver.status_name(status)
    objective_units: int | None = None
    upper_bound_units: int | None = None
    selected_contract_ids: tuple[int, ...] = ()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        objective_units = int(solver.value(master.total_reward_units))
        upper_bound_units = (
            objective_units
            if status == cp_model.OPTIMAL
            else max(
                objective_units,
                _integer_upper_bound(solver.best_objective_bound),
            )
        )
        selected_contract_ids = tuple(
            sorted(
                contract_id
                for contract_id, is_contract_selected in master.contract_is_selected.items()
                if solver.value(is_contract_selected)
            )
        )
    elif status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT rejected the validated system-relaxation model")

    return SystemRelaxationBound(
        status_name=status_name,
        upper_bound_units=upper_bound_units,
        objective_units=objective_units,
        wall_time_seconds=solver.wall_time,
        branches=solver.num_branches,
        conflicts=solver.num_conflicts,
        routed_systems=master.routed_systems,
        selected_contract_ids=selected_contract_ids,
    )


def solve_system_relaxation(
    prepared: PreparedProblem,
    *,
    max_time_seconds: float,
    random_seed: int = 0,
    selection_cuts: SelectionCuts | None = None,
) -> SystemRelaxationBound:
    """Build and solve one endpoint-system relaxation for callers that do not need reuse."""

    master = build_system_relaxation_master(prepared, selection_cuts=selection_cuts)
    return solve_system_relaxation_master(
        master,
        max_time_seconds=max_time_seconds,
        random_seed=random_seed,
    )


def _pair_minimum_seconds(
    prepared: PreparedProblem,
    first: RoutableContract,
    second: RoutableContract,
) -> int | None:
    """Optimistic resource-feasible duration for servicing exactly two contracts."""

    constraints = prepared.problem.constraints
    contracts = (first, second)
    # Event indexes are first pickup/delivery (0/1) and second pickup/delivery (2/3). These are
    # the six possible orders that preserve pickup-before-delivery for both contracts.
    valid_event_orders = (
        (0, 1, 2, 3),
        (0, 2, 1, 3),
        (0, 2, 3, 1),
        (2, 3, 0, 1),
        (2, 0, 3, 1),
        (2, 0, 1, 3),
    )
    minimum_route_seconds: int | None = None
    for event_order in valid_event_orders:
        current_system_id = constraints.start_system_id
        total_jump_count = 0
        cargo_load_units = 0
        active_contract_count = 0
        locked_collateral_units = 0
        is_feasible = True
        for event_index in event_order:
            contract = contracts[event_index // 2]
            is_pickup = event_index % 2 == 0
            if is_pickup:
                cargo_load_units += contract.contract.volume_units
                active_contract_count += 1
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    locked_collateral_units += contract.contract.collateral_units
                target_system_id = contract.origin_system_id
            else:
                cargo_load_units -= contract.contract.volume_units
                active_contract_count -= 1
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    locked_collateral_units -= contract.contract.collateral_units
                target_system_id = contract.destination_system_id
            if cargo_load_units > constraints.cargo_capacity_units:
                is_feasible = False
                break
            if (
                constraints.max_simultaneous_contracts is not None
                and active_contract_count > constraints.max_simultaneous_contracts
            ):
                is_feasible = False
                break
            if locked_collateral_units > constraints.collateral_budget_units:
                is_feasible = False
                break
            leg_jump_count = prepared.jump_matrix.get((current_system_id, target_system_id))
            if leg_jump_count is None:
                is_feasible = False
                break
            total_jump_count += leg_jump_count
            current_system_id = target_system_id
        terminal_system_id = constraints.terminal_system_id
        if is_feasible and terminal_system_id is not None:
            finish_jump_count = prepared.jump_matrix.get((current_system_id, terminal_system_id))
            if finish_jump_count is None:
                is_feasible = False
            else:
                total_jump_count += finish_jump_count
        if is_feasible:
            route_time_seconds = (
                total_jump_count * constraints.travel.seconds_per_jump
                + 4 * constraints.travel.service_seconds
            )
            minimum_route_seconds = (
                route_time_seconds
                if minimum_route_seconds is None
                else min(minimum_route_seconds, route_time_seconds)
            )
    return minimum_route_seconds


def _greedy_cliques(
    contract_ids: tuple[int, ...],
    pairs: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, ...], ...]:
    """Find deterministic valid cliques without an exponential maximal-clique search."""

    adjacent: dict[int, set[int]] = {contract_id: set() for contract_id in contract_ids}
    for first, second in pairs:
        adjacent[first].add(second)
        adjacent[second].add(first)
    ordered = sorted(
        contract_ids, key=lambda contract_id: (-len(adjacent[contract_id]), contract_id)
    )
    found: set[tuple[int, ...]] = set()
    for seed in ordered:
        candidate_clique = [seed]
        candidates = sorted(
            adjacent[seed], key=lambda contract_id: (-len(adjacent[contract_id]), contract_id)
        )
        for candidate in candidates:
            if all(candidate in adjacent[member] for member in candidate_clique):
                candidate_clique.append(candidate)
        if len(candidate_clique) >= 3:
            found.add(tuple(sorted(candidate_clique)))
    maximal: list[tuple[int, ...]] = []
    for found_clique in sorted(found, key=lambda item: (-len(item), item)):
        members = set(found_clique)
        if not any(members <= set(existing) for existing in maximal):
            maximal.append(found_clique)
    return tuple(maximal)


def build_selection_cuts(prepared: PreparedProblem) -> SelectionCuts:
    """Derive pair and clique incompatibilities from optimistic necessary conditions."""

    problem = prepared.problem
    constraints = problem.constraints
    contract_ids = tuple(contract.contract.contract_id for contract in problem.contracts)
    contract_by_id = {contract.contract.contract_id: contract for contract in problem.contracts}
    active_collateral_units = _active_collateral(prepared)
    incompatible_contract_pairs: list[tuple[int, int]] = []
    for first_id, second_id in combinations(contract_ids, 2):
        collateral_conflict = False
        if constraints.collateral_mode is CollateralMode.LOCKED:
            collateral_conflict = (
                active_collateral_units
                + contract_by_id[first_id].contract.collateral_units
                + contract_by_id[second_id].contract.collateral_units
                > constraints.collateral_budget_units
            )
        minimum_seconds = _pair_minimum_seconds(
            prepared,
            contract_by_id[first_id],
            contract_by_id[second_id],
        )
        time_conflict = minimum_seconds is None or minimum_seconds > constraints.horizon_seconds
        if collateral_conflict or time_conflict:
            incompatible_contract_pairs.append((first_id, second_id))
    incompatible_pairs = tuple(incompatible_contract_pairs)
    return SelectionCuts(
        pairs=incompatible_pairs,
        cliques=_greedy_cliques(contract_ids, incompatible_pairs),
    )
