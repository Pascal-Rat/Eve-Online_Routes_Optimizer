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
    selected: dict[int, cp_model.IntVar]
    total_reward: cp_model.IntVar
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


def _relaxation_systems(prepared: PreparedProblem) -> tuple[set[int], set[int]]:
    """Return all endpoint systems and the subset that a real route must visit."""

    problem = prepared.problem
    constraints = problem.constraints
    systems = set(constraints.required_system_ids)
    mandatory = set(constraints.required_system_ids)
    for item in problem.contracts:
        systems.add(item.origin_system_id)
        systems.add(item.destination_system_id)
    for shipment in problem.active_shipments:
        if not shipment.picked:
            systems.add(shipment.contract.origin_system_id)
            mandatory.add(shipment.contract.origin_system_id)
        systems.add(shipment.contract.destination_system_id)
        mandatory.add(shipment.contract.destination_system_id)
    systems.add(constraints.start_system_id)
    if constraints.terminal_system_id is not None:
        systems.add(constraints.terminal_system_id)
        mandatory.add(constraints.terminal_system_id)
    return systems, mandatory


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
    all_systems, mandatory_systems = _relaxation_systems(prepared)
    start_system = constraints.start_system_id
    terminal_system = constraints.terminal_system_id
    intermediate_systems = tuple(
        sorted(all_systems - {start_system} - ({terminal_system} if terminal_system else set()))
    )
    node_system: dict[int, int | None] = {0: start_system, 1: terminal_system}
    node_for_system: dict[int, int] = {}
    for node, system_id in enumerate(intermediate_systems, start=2):
        node_system[node] = system_id
        node_for_system[system_id] = node

    model = cp_model.CpModel()
    selected = {
        item.contract.contract_id: model.new_bool_var(f"relax_select_{item.contract.contract_id}")
        for item in problem.contracts
    }
    cuts = selection_cuts if selection_cuts is not None else build_selection_cuts(prepared)
    clique_pairs = {
        tuple(sorted(pair)) for clique in cuts.cliques for pair in combinations(clique, 2)
    }
    for clique in cuts.cliques:
        model.add(sum(selected[contract_id] for contract_id in clique) <= 1)
    for first, second in cuts.pairs:
        if (first, second) not in clique_pairs:
            model.add(selected[first] + selected[second] <= 1)
    visit = {
        system_id: model.new_bool_var(f"relax_visit_{system_id}")
        for system_id in intermediate_systems
    }

    incident: dict[int, list[cp_model.IntVar]] = {
        system_id: [] for system_id in intermediate_systems
    }
    for item in problem.contracts:
        literal = selected[item.contract.contract_id]
        for system_id in {item.origin_system_id, item.destination_system_id}:
            if system_id in visit:
                model.add(literal <= visit[system_id])
                incident[system_id].append(literal)
    for system_id, literal in visit.items():
        if system_id in mandatory_systems:
            model.add(literal == 1)
        elif incident[system_id]:
            model.add(literal <= sum(incident[system_id]))
        else:
            model.add(literal == 0)

    route_arcs: dict[tuple[int, int], cp_model.IntVar] = {}
    circuit_arcs: list[tuple[int, int, cp_model.IntVar]] = []
    wrap = model.new_bool_var("relax_end_to_start")
    model.add(wrap == 1)
    route_arcs[(1, 0)] = wrap
    circuit_arcs.append((1, 0, wrap))
    for system_id, node in node_for_system.items():
        skip = model.new_bool_var(f"relax_skip_{system_id}")
        model.add(skip + visit[system_id] == 1)
        circuit_arcs.append((node, node, skip))

    travel_terms: list[cp_model.LinearExpr] = []
    nodes = tuple(sorted(node_system))
    for from_node in nodes:
        if from_node == 1:
            continue
        from_system = node_system[from_node]
        assert from_system is not None
        for to_node in nodes:
            if to_node == 0 or to_node == from_node:
                continue
            to_system = node_system[to_node]
            if to_node == 1 and to_system is None:
                jumps = 0
            elif to_system is None:
                continue
            else:
                distance = prepared.jump_matrix.get((from_system, to_system))
                if distance is None:
                    continue
                jumps = distance
            literal = model.new_bool_var(f"relax_arc_{from_node}_{to_node}")
            route_arcs[(from_node, to_node)] = literal
            circuit_arcs.append((from_node, to_node, literal))
            seconds = jumps * constraints.travel.seconds_per_jump
            if seconds:
                travel_terms.append(seconds * literal)
    model.add_circuit(circuit_arcs)

    selected_count = sum(selected.values())
    service_seconds = constraints.travel.service_seconds * (
        _mandatory_action_count(prepared) + 2 * selected_count
    )
    model.add(sum(travel_terms) + service_seconds <= constraints.horizon_seconds)

    if constraints.collateral_mode is CollateralMode.LOCKED:
        model.add(
            _active_collateral(prepared)
            + sum(
                item.contract.collateral_units * selected[item.contract.contract_id]
                for item in problem.contracts
            )
            <= constraints.collateral_budget_units
        )

    active_reward = _active_reward(prepared)
    maximum_reward = active_reward + sum(item.contract.reward_units for item in problem.contracts)
    total_reward = model.new_int_var(active_reward, maximum_reward, "relax_total_reward")
    model.add(
        total_reward
        == active_reward
        + sum(
            item.contract.reward_units * selected[item.contract.contract_id]
            for item in problem.contracts
        )
    )
    model.maximize(total_reward)

    validation_error = model.validate()
    if validation_error:
        raise ValueError(f"invalid system-relaxation model: {validation_error}")
    return SystemRelaxationMaster(
        model=model,
        selected=selected,
        total_reward=total_reward,
        routed_systems=len(all_systems),
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

    normalized = tuple(sorted(set(contract_ids)))
    if not normalized:
        raise ValueError("an infeasible selection cut must contain at least one contract")
    unknown = tuple(contract_id for contract_id in normalized if contract_id not in master.selected)
    if unknown:
        raise ValueError(f"selection cut contains unknown contract IDs: {unknown}")
    master.model.add(
        sum(master.selected[contract_id] for contract_id in normalized) <= len(normalized) - 1
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
    objective: int | None = None
    upper_bound: int | None = None
    selected_contract_ids: tuple[int, ...] = ()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        objective = int(solver.value(master.total_reward))
        upper_bound = (
            objective
            if status == cp_model.OPTIMAL
            else max(objective, _integer_upper_bound(solver.best_objective_bound))
        )
        selected_contract_ids = tuple(
            sorted(
                contract_id
                for contract_id, literal in master.selected.items()
                if solver.value(literal)
            )
        )
    elif status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT rejected the validated system-relaxation model")

    return SystemRelaxationBound(
        status_name=status_name,
        upper_bound_units=upper_bound,
        objective_units=objective,
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
    items = (first, second)
    sequences = (
        (0, 1, 2, 3),
        (0, 2, 1, 3),
        (0, 2, 3, 1),
        (2, 3, 0, 1),
        (2, 0, 3, 1),
        (2, 0, 1, 3),
    )
    best: int | None = None
    for sequence in sequences:
        current = constraints.start_system_id
        jumps = 0
        cargo = 0
        parcels = 0
        rolling_collateral = 0
        reachable = True
        for event in sequence:
            item = items[event // 2]
            pickup = event % 2 == 0
            if pickup:
                cargo += item.contract.volume_units
                parcels += 1
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    rolling_collateral += item.contract.collateral_units
                system_id = item.origin_system_id
            else:
                cargo -= item.contract.volume_units
                parcels -= 1
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    rolling_collateral -= item.contract.collateral_units
                system_id = item.destination_system_id
            if cargo > constraints.cargo_capacity_units:
                reachable = False
                break
            if (
                constraints.max_simultaneous_contracts is not None
                and parcels > constraints.max_simultaneous_contracts
            ):
                reachable = False
                break
            if rolling_collateral > constraints.collateral_budget_units:
                reachable = False
                break
            distance = prepared.jump_matrix.get((current, system_id))
            if distance is None:
                reachable = False
                break
            jumps += distance
            current = system_id
        terminal = constraints.terminal_system_id
        if reachable and terminal is not None:
            distance = prepared.jump_matrix.get((current, terminal))
            if distance is None:
                reachable = False
            else:
                jumps += distance
        if reachable:
            seconds = (
                jumps * constraints.travel.seconds_per_jump + 4 * constraints.travel.service_seconds
            )
            best = seconds if best is None else min(best, seconds)
    return best


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
    contract_ids = tuple(item.contract.contract_id for item in problem.contracts)
    by_id = {item.contract.contract_id: item for item in problem.contracts}
    active_collateral = _active_collateral(prepared)
    pairs: list[tuple[int, int]] = []
    for first_id, second_id in combinations(contract_ids, 2):
        collateral_conflict = False
        if constraints.collateral_mode is CollateralMode.LOCKED:
            collateral_conflict = (
                active_collateral
                + by_id[first_id].contract.collateral_units
                + by_id[second_id].contract.collateral_units
                > constraints.collateral_budget_units
            )
        minimum_seconds = _pair_minimum_seconds(
            prepared,
            by_id[first_id],
            by_id[second_id],
        )
        time_conflict = minimum_seconds is None or minimum_seconds > constraints.horizon_seconds
        if collateral_conflict or time_conflict:
            pairs.append((first_id, second_id))
    pair_tuple = tuple(pairs)
    return SelectionCuts(
        pairs=pair_tuple,
        cliques=_greedy_cliques(contract_ids, pair_tuple),
    )
