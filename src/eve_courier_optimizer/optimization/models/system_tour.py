"""Relax pickup/delivery order into a system tour to bound achievable courier reward."""

from __future__ import annotations

from dataclasses import dataclass

from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import CollateralMode
from eve_courier_optimizer.optimization.models.selection_bounds import (
    ResourceCrossings,
    RewardBoundRecorder,
    SelectionCuts,
    add_resource_crossing_bounds,
    add_resource_work_bounds,
    add_selection_cuts,
    build_selection_cuts,
    integer_upper_bound,
)
from eve_courier_optimizer.routing.route_problem import RouteProblem


@dataclass(frozen=True, slots=True)
class SystemRewardBound:
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
class SystemTourModel:
    """A system tour whose optimal reward bounds the full pickup/delivery problem."""

    model: cp_model.CpModel
    contract_is_selected: dict[int, cp_model.IntVar]
    total_reward_units: cp_model.IntVar
    routed_systems: int
    system_id_by_node_id: dict[int, int | None]
    arc_is_used: dict[tuple[int, int], cp_model.IntVar]
    system_is_visited: dict[int, cp_model.IntVar]
    system_is_skipped: dict[int, cp_model.IntVar]
    resource_crossings: tuple[ResourceCrossings, ...]
    start_system_id: int
    terminal_system_id: int | None

    def __init__(
        self, problem: RouteProblem, *, selection_cuts: SelectionCuts | None = None
    ) -> None:
        """Build the endpoint-system route relaxation while retaining its selection literals.

        Every exact route maps here by shortcutting repeated endpoint visits through the metric
        closure. Retaining visitation, total service time, the horizon and locked collateral while
        dropping action order, cargo, parcel state, rolling collateral and individual deadlines
        makes the optimum a valid reward ceiling for the exact courier problem.
        """

        constraints = problem.constraints
        candidate_system_ids, mandatory_system_ids = _collect_relaxation_system_ids(problem)
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
            contract.contract_id: model.new_bool_var(f"relax_select_{contract.contract_id}")
            for contract in problem.contracts
        }
        effective_selection_cuts = (
            selection_cuts if selection_cuts is not None else build_selection_cuts(problem)
        )
        add_selection_cuts(model, contract_is_selected, effective_selection_cuts)
        system_is_visited = {
            system_id: model.new_bool_var(f"relax_visit_{system_id}")
            for system_id in intermediate_system_ids
        }

        selection_literals_incident_to_system: dict[int, list[cp_model.IntVar]] = {
            system_id: [] for system_id in intermediate_system_ids
        }
        for contract in problem.contracts:
            is_contract_selected = contract_is_selected[contract.contract_id]
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
                model.add(
                    is_system_visited <= sum(selection_literals_incident_to_system[system_id])
                )
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

        arc_is_used = {(u, v): literal for u, v, literal in circuit_arc_definitions}
        skipped = {system: arc_is_used[node, node] for system, node in node_id_by_system_id.items()}
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
                    possible_jump_count = problem.jump_matrix.get(
                        (source_system_id, destination_system_id)
                    )
                    if possible_jump_count is None:
                        continue
                    jump_count = possible_jump_count
                is_arc_used = model.new_bool_var(
                    f"relax_arc_{source_node_id}_{destination_node_id}"
                )
                arc_is_used[source_node_id, destination_node_id] = is_arc_used
                circuit_arc_definitions.append((source_node_id, destination_node_id, is_arc_used))
                travel_time_seconds = jump_count * constraints.travel.seconds_per_jump
                if travel_time_seconds:
                    travel_time_terms.append(travel_time_seconds * is_arc_used)
        model.add_circuit(circuit_arc_definitions)

        selected_contract_count = sum(contract_is_selected.values())
        service_time_seconds = constraints.travel.service_seconds * (
            problem.mandatory_action_count + 2 * selected_contract_count
        )
        model.add(sum(travel_time_terms) + service_time_seconds <= constraints.horizon_seconds)

        if constraints.collateral_mode is CollateralMode.LOCKED:
            model.add(
                problem.initial_collateral_units
                + sum(
                    contract.collateral_units * contract_is_selected[contract.contract_id]
                    for contract in problem.contracts
                )
                <= constraints.collateral_budget_units
            )

        committed_reward_units = problem.committed_reward_units
        maximum_reward_units = committed_reward_units + sum(
            contract.reward_units for contract in problem.contracts
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
                contract.reward_units * contract_is_selected[contract.contract_id]
                for contract in problem.contracts
            )
        )
        add_resource_work_bounds(model, problem, contract_is_selected)
        self.resource_crossings = add_resource_crossing_bounds(model, problem, contract_is_selected)
        self.start_system_id = start_system_id
        self.terminal_system_id = terminal_system_id
        model.maximize(total_reward_units)

        validation_error = model.validate()
        if validation_error:
            raise ValueError(f"invalid system-relaxation model: {validation_error}")
        self.model = model
        self.contract_is_selected = contract_is_selected
        self.total_reward_units = total_reward_units
        self.routed_systems = len(candidate_system_ids)
        self.system_id_by_node_id = system_id_by_node_id
        self.arc_is_used = arc_is_used
        self.system_is_visited = system_is_visited
        self.system_is_skipped = skipped

    def hint(
        self, contract_ids: tuple[int, ...], system_order: tuple[int, ...], reward: int
    ) -> None:
        """Shortcut an independently feasible route into a complete master hint."""
        node_by_system = {
            system: node for node, system in self.system_id_by_node_id.items() if node >= 2
        }
        nodes = [0]
        for system in system_order:
            node = node_by_system.get(system)
            if node is not None and node not in nodes:
                nodes.append(node)
        nodes.append(1)
        arcs = set(zip(nodes, nodes[1:], strict=False)) | {(1, 0)}
        if not arcs <= self.arc_is_used.keys():
            raise RuntimeError("verified incumbent cannot be projected into the system master")
        self.model.clear_hints()  # type: ignore[no-untyped-call]
        for contract_id, variable in self.contract_is_selected.items():
            self.model.add_hint(variable, int(contract_id in contract_ids))
        for system, variable in self.system_is_visited.items():
            visited = int(node_by_system[system] in nodes)
            self.model.add_hint(variable, visited)
            self.model.add_hint(self.system_is_skipped[system], 1 - visited)
        for arc, variable in self.arc_is_used.items():
            # Optional self-loops are already hinted through system_is_skipped.
            if arc[0] != arc[1]:
                self.model.add_hint(variable, int(arc in arcs))
        self.model.add_hint(self.total_reward_units, reward)
        full_order = (self.start_system_id, *system_order)
        if self.terminal_system_id is not None and full_order[-1] != self.terminal_system_id:
            full_order = (*full_order, self.terminal_system_id)
        for crossings in self.resource_crossings:
            crossings.hint(self.model, full_order)
        self.model.add(self.total_reward_units >= reward)

    def exclude_infeasible_selection(self, contract_ids: tuple[int, ...]) -> None:
        """Forbid one rigorously proven infeasible coexistence set in the self.

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
            if contract_id not in self.contract_is_selected
        )
        if unknown_contract_ids:
            raise ValueError(f"selection cut contains unknown contract IDs: {unknown_contract_ids}")
        self.model.add(
            sum(self.contract_is_selected[contract_id] for contract_id in normalized_contract_ids)
            <= len(normalized_contract_ids) - 1
        )

    def solve(self, *, max_time_seconds: float, random_seed: int = 0) -> SystemRewardBound:
        """Solve a reusable system master and expose both its ceiling and chosen contracts."""

        if max_time_seconds <= 0:
            raise ValueError("system-relaxation time limit must be positive")
        validation_error = self.model.validate()
        if validation_error:
            raise ValueError(f"invalid system-relaxation model: {validation_error}")
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max_time_seconds
        # Single-worker search avoids portfolio overhead on this smaller bound model.
        solver.parameters.num_search_workers = 1
        # The master is mostly Boolean selection and circuit decisions. Include their
        # Boolean constraints in the LP, so it can tighten the reward ceiling before
        # branching on complete tours. Keep the event oracle's settings independent.
        solver.parameters.linearization_level = 2
        solver.parameters.random_seed = random_seed
        bounds = RewardBoundRecorder()
        solver.best_bound_callback = bounds
        status = solver.solve(self.model)
        status_name = solver.status_name(status)
        objective_units: int | None = None
        upper_bound_units: int | None = None
        selected_contract_ids: tuple[int, ...] = ()
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            objective_units = int(solver.value(self.total_reward_units))
            upper_bound_units = (
                objective_units
                if status == cp_model.OPTIMAL
                else max(
                    objective_units,
                    integer_upper_bound(solver.best_objective_bound),
                )
            )
            selected_contract_ids = tuple(
                sorted(
                    contract_id
                    for contract_id, is_contract_selected in self.contract_is_selected.items()
                    if solver.value(is_contract_selected)
                )
            )
        elif status == cp_model.MODEL_INVALID:
            raise ValueError("CP-SAT rejected the validated system-relaxation model")
        elif status == cp_model.UNKNOWN:
            upper_bound_units = bounds.upper_bound_units

        return SystemRewardBound(
            status_name=status_name,
            upper_bound_units=upper_bound_units,
            objective_units=objective_units,
            wall_time_seconds=solver.wall_time,
            branches=solver.num_branches,
            conflicts=solver.num_conflicts,
            routed_systems=self.routed_systems,
            selected_contract_ids=selected_contract_ids,
        )


def _collect_relaxation_system_ids(
    problem: RouteProblem,
) -> tuple[set[int], set[int]]:
    """Return all endpoint systems and the subset that a real route must visit."""

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
