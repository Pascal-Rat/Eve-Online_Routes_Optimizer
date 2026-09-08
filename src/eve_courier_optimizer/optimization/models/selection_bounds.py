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

from eve_courier_optimizer.domain import CollateralMode, RoutableContract
from eve_courier_optimizer.routing.route_problem import RouteProblem


@dataclass(frozen=True, slots=True)
class SelectionCuts:
    """Redundant incompatibility constraints expressed only over contract selection."""

    pairs: tuple[tuple[int, int], ...]
    cliques: tuple[tuple[int, ...], ...]


@dataclass(frozen=True, slots=True)
class SubsetRewardCut:
    """A certified optional-reward ceiling on a subset of the original candidate pool."""

    terms: tuple[tuple[int, int], ...]
    upper_bound_units: int


def add_subset_reward_cut(
    model: cp_model.CpModel,
    selected: dict[int, cp_model.IntVar],
    cut: SubsetRewardCut,
) -> None:
    """Projection onto this subset preserves feasibility; its exact ceiling applies globally."""
    model.add(sum(reward * selected[cid] for cid, reward in cut.terms) <= cut.upper_bound_units)


def add_selection_cuts(
    model: cp_model.CpModel,
    selected: dict[int, cp_model.IntVar],
    cuts: SelectionCuts,
) -> None:
    covered_pairs = {
        tuple(sorted(pair)) for clique in cuts.cliques for pair in combinations(clique, 2)
    }
    for clique in cuts.cliques:
        model.add(sum(selected[contract_id] for contract_id in clique) <= 1)
    for first, second in cuts.pairs:
        if (first, second) not in covered_pairs:
            model.add(selected[first] + selected[second] <= 1)


def integer_upper_bound(raw_bound: float) -> int:
    """Conservatively convert CP-SAT's double objective bound to an integer ceiling."""

    if abs(raw_bound) <= 2**53 - 1:
        return int(math.ceil(raw_bound))
    return int(math.ceil(math.nextafter(raw_bound, math.inf)))


@dataclass(slots=True)
class RewardBoundRecorder:
    """Retain explicit CP-SAT bound events for one integer reward maximization.

    An UNKNOWN response can contain a default zero if stopped before search initialization.
    Only an actual bound callback certifies progress in that case. OR-Tools serializes these
    callbacks under its response-manager mutex. Use a fresh recorder for each reward solve;
    duration minimization and fixed-selection feasibility have different bound meanings.
    """

    upper_bound_units: int | None = None

    def __call__(self, raw_bound: float) -> None:
        if not math.isfinite(raw_bound):
            return
        bound = integer_upper_bound(raw_bound)
        if self.upper_bound_units is None or bound < self.upper_bound_units:
            self.upper_bound_units = bound


@dataclass(frozen=True, slots=True)
class _ResourceWorkSpec:
    capacity: int
    resource: str
    threshold: int | None = None

    def demand(self, value: int) -> int:
        return value if self.threshold is None else int(value > self.threshold)


class _LiftedResourceWorkSpec(_ResourceWorkSpec):
    """Integer form of the dual-feasible U(epsilon) packing transform.

    For 0 < t <= C/2, discard loads below t and raise loads above C-t to C. A feasible
    packing with a raised load has only discarded companions; without one, every transformed
    load is at most its original size. Thus transformed co-carried demand still never exceeds C.
    Strict inequalities preserve exact-fit packings at both boundaries.
    """

    def demand(self, value: int) -> int:
        assert self.threshold is not None
        if value < self.threshold:
            return 0
        return self.capacity if value > self.capacity - self.threshold else value


@dataclass(frozen=True, slots=True)
class ResourceCrossings:
    """Integer crossing counts for one distance potential, including complete route hints."""

    potential: dict[int, int]
    layers: tuple[tuple[int, cp_model.IntVar, cp_model.IntVar], ...]

    def hint(self, model: cp_model.CpModel, system_order: tuple[int, ...]) -> None:
        for level, outward, inward in self.layers:
            sides = tuple(self.potential[system] >= level for system in system_order)
            model.add_hint(
                outward, sum(not a and b for a, b in zip(sides, sides[1:], strict=False))
            )
            model.add_hint(inward, sum(a and not b for a, b in zip(sides, sides[1:], strict=False)))


def add_resource_crossing_bounds(
    model: cp_model.CpModel,
    problem: RouteProblem,
    selected: dict[int, cp_model.IntVar],
) -> tuple[ResourceCrossings, ...]:
    """Count whole capacity-limited trips across distance layers of a symmetric metric.

    Each parcel whose endpoints lie on opposite sides must cross while carried. For each
    resource, demand <= capacity * integer crossings. Outward minus inward crossings equals
    the terminal-side change (or either possible terminal side for a free finish). A shortest
    distance potential changes by at most one per jump, so the sum of crossings over disjoint
    layers cannot exceed actual travel. Different potentials each bound that same travel;
    their bounds must never be added together or charged against the shorter master circuit.

    Consecutive layers with no endpoint between them have identical required demand and
    terminal balance. Their common minimum crossing count may be weighted by their width.
    Start/terminal potentials are structural choices independent of benchmark identities.
    """
    c = problem.constraints
    if any(d != problem.jump_matrix.get((b, a)) for (a, b), d in problem.jump_matrix.items()):
        return ()
    # Capacity and (optional selection ID, transformed demand, source, destination).
    resources: list[tuple[int, tuple[tuple[int | None, int, int, int], ...]]] = []
    systems = {c.start_system_id, *c.required_system_ids}
    for spec in _resource_work_specs(problem):
        if spec.capacity <= 0:
            continue
        shipments: list[tuple[int | None, int, int, int]] = []
        for item in problem.contracts:
            value = (
                item.volume_units
                if spec.resource == "volume"
                else item.collateral_units
                if spec.resource == "collateral"
                else 1
            )
            shipments.append(
                (
                    item.contract_id,
                    spec.demand(value),
                    item.origin_system_id,
                    item.destination_system_id,
                )
            )
        for active in problem.active_shipments:
            item = active.contract
            value = (
                item.volume_units
                if spec.resource == "volume"
                else item.collateral_units
                if spec.resource == "collateral"
                else 1
            )
            origin = (
                c.start_system_id
                if active.picked or spec.resource == "collateral"
                else item.origin_system_id
            )
            shipments.append((None, spec.demand(value), origin, item.destination_system_id))
        for _, _, source, destination in shipments:
            systems.update((source, destination))
        resources.append((spec.capacity, tuple(shipments)))
    if not resources:
        return ()
    if c.terminal_system_id is not None:
        systems.add(c.terminal_system_id)
    service = c.travel.service_seconds * (
        problem.mandatory_action_count + 2 * sum(selected.values())
    )
    max_crossings = c.horizon_seconds // c.travel.seconds_per_jump
    result: list[ResourceCrossings] = []
    pivots = {c.start_system_id}
    if c.terminal_system_id is not None:
        pivots.add(c.terminal_system_id)
    for pivot in sorted(pivots):
        distances = {s: problem.jump_matrix.get((pivot, s)) for s in systems}
        if any(distance is None for distance in distances.values()):
            continue
        potential = {s: d for s, d in distances.items() if d is not None}
        levels = sorted(set(potential.values()))
        expression_magnitude = (
            2 * (levels[-1] - levels[0]) * max_crossings * c.travel.seconds_per_jump
            + c.travel.service_seconds * (problem.mandatory_action_count + 2 * len(selected))
            + c.horizon_seconds
        )
        if expression_magnitude >= 2**62:
            continue
        layers: list[tuple[int, cp_model.IntVar, cp_model.IntVar]] = []
        travel_terms: list[cp_model.LinearExpr] = []
        for low, high in zip(levels, levels[1:], strict=False):
            outward = model.new_int_var(0, max_crossings, f"cross_out_{pivot}_{high}")
            inward = model.new_int_var(0, max_crossings, f"cross_in_{pivot}_{high}")
            layers.append((high, outward, inward))
            travel_terms.append((high - low) * (outward + inward))
            start_side = int(potential[c.start_system_id] >= high)
            if c.terminal_system_id is not None:
                model.add(
                    outward - inward == int(potential[c.terminal_system_id] >= high) - start_side
                )
            else:
                model.add(outward - inward >= -start_side)
                model.add(outward - inward <= 1 - start_side)
            seen: set[tuple[bool, int, tuple[tuple[int | None, int], ...]]] = set()
            for capacity, resource_shipments in resources:
                for source_side, crossings in ((False, outward), (True, inward)):
                    demand = tuple(
                        (cid, value)
                        for cid, value, source, destination in resource_shipments
                        if value
                        and (potential[source] >= high) == source_side
                        and (potential[destination] >= high) != source_side
                    )
                    divisor = math.gcd(capacity, *(value for _, value in demand))
                    scaled_capacity = capacity // divisor
                    signature = (
                        source_side,
                        scaled_capacity,
                        tuple((cid, v // divisor) for cid, v in demand),
                    )
                    if not demand or signature in seen:
                        continue
                    seen.add(signature)
                    # Optional strengthening must not overflow an otherwise valid model.
                    if (
                        sum(v // divisor for _, v in demand) + scaled_capacity * max_crossings
                        >= 2**62
                    ):
                        continue
                    model.add(
                        sum(
                            v // divisor * (1 if cid is None else selected[cid])
                            for cid, v in demand
                        )
                        <= scaled_capacity * crossings
                    )
        if layers:
            model.add(c.travel.seconds_per_jump * sum(travel_terms) + service <= c.horizon_seconds)
            result.append(ResourceCrossings(potential, tuple(layers)))
    return tuple(result)


def _resource_work_specs(problem: RouteProblem) -> list[_ResourceWorkSpec]:
    c = problem.constraints
    resources = [(c.cargo_capacity_units, "volume")]
    if c.max_simultaneous_contracts is not None:
        resources.append((c.max_simultaneous_contracts, "parcels"))
    if c.collateral_mode is CollateralMode.ROLLING:
        resources.append((c.collateral_budget_units, "collateral"))
    capacity_specs = [_ResourceWorkSpec(capacity, resource) for capacity, resource in resources]
    # At most k items larger than C/(k+1) fit simultaneously. Their count is another
    # valid transport resource, capturing indivisible parcels that fractional volume misses.
    capacity_specs.extend(
        _ResourceWorkSpec(k, resource, capacity // (k + 1))
        for capacity, resource in resources
        if capacity > 0 and resource != "parcels"
        for k in (1, 2, 3)
    )
    capacity_specs.extend(
        _LiftedResourceWorkSpec(capacity, resource, capacity // k)
        for capacity, resource in resources
        if resource != "parcels"
        for k in (2, 3, 4)
        if capacity // k > 0
    )
    return capacity_specs


def add_resource_work_bounds(
    model: cp_model.CpModel,
    problem: RouteProblem,
    selected: dict[int, cp_model.IntVar],
) -> int:
    """Bound transport work by capacity and available travel, without fixing a route.

    For any 1-Lipschitz potential f, an item from p to d needs at least max(f(d)-f(p), 0)
    positive progress. At every instant the total carried resource is at most C. For a fixed
    terminal, positive progress of the whole route is at most (travel + f(end)-f(start))/2.
    This gives a necessary selection inequality even when the system master shortcuts revisits.
    Crucially we use the AVAILABLE travel budget, not the master's shorter selected circuit.
    """
    c = problem.constraints
    service = c.travel.service_seconds
    jump_seconds = c.travel.seconds_per_jump
    mandatory_service = problem.mandatory_action_count * service
    symmetric_metric = all(
        distance == problem.jump_matrix.get((destination, source))
        for (source, destination), distance in problem.jump_matrix.items()
    )
    pivots = sorted(
        {c.start_system_id}
        | {
            endpoint
            for item in problem.contracts
            for endpoint in (item.origin_system_id, item.destination_system_id)
        }
    )
    count = 0
    seen: set[tuple[tuple[int, ...], int]] = set()
    ids = tuple(selected)
    for spec in _resource_work_specs(problem):
        capacity, resource = spec.capacity, spec.resource
        if capacity <= 0:
            continue
        # (selection ID, demand, source, destination); None denotes a mandatory shipment.
        shipments: list[tuple[int | None, int, int, int]] = []
        for item in problem.contracts:
            demand = (
                item.volume_units
                if resource == "volume"
                else item.collateral_units
                if resource == "collateral"
                else 1
            )
            demand = spec.demand(demand)
            shipments.append(
                (
                    item.contract_id,
                    demand,
                    item.origin_system_id,
                    item.destination_system_id,
                )
            )
        for active in problem.active_shipments:
            item = active.contract
            demand = (
                item.volume_units
                if resource == "volume"
                else item.collateral_units
                if resource == "collateral"
                else 1
            )
            origin = (
                c.start_system_id
                if active.picked or resource == "collateral"
                else item.origin_system_id
            )
            demand = spec.demand(demand)
            shipments.append((None, demand, origin, item.destination_system_id))
        divisor = math.gcd(capacity, *(row[1] for row in shipments))
        scaled_capacity = capacity // divisor
        # Direct metric transport work also applies to fully open routes.
        distances: list[tuple[int, int, list[int | None]]] = [
            (1, 0, [problem.jump_matrix.get((s, d)) for _, _, s, d in shipments])
        ]
        if c.terminal_system_id is not None and symmetric_metric:
            for pivot in pivots:
                start = problem.jump_matrix.get((pivot, c.start_system_id))
                finish = problem.jump_matrix.get((pivot, c.terminal_system_id))
                values = [
                    (problem.jump_matrix.get((pivot, s)), problem.jump_matrix.get((pivot, d)))
                    for _, _, s, d in shipments
                ]
                if (
                    start is None
                    or finish is None
                    or any(s is None or d is None for s, d in values)
                ):
                    continue
                for sign in (1, -1):
                    progress: list[int | None] = [
                        max(0, sign * (d - s)) for s, d in values if s is not None and d is not None
                    ]
                    distances.append((2, sign * (finish - start), progress))
        for multiplier, terminal_delta, travel in distances:
            if any(value is None for value in travel):
                continue
            coefficients = {i: 2 * service * scaled_capacity for i in ids}
            mandatory_work = 0
            for (contract_id, demand, _, _), distance in zip(shipments, travel, strict=True):
                assert distance is not None
                work = multiplier * (demand // divisor) * distance * jump_seconds
                if contract_id is None:
                    mandatory_work += work
                else:
                    coefficients[contract_id] += work
            rhs = (
                scaled_capacity
                * (c.horizon_seconds - mandatory_service + terminal_delta * jump_seconds)
                - mandatory_work
            )
            divisor_row = math.gcd(*(coefficients.values()), rhs)
            if divisor_row:
                coefficients = {i: value // divisor_row for i, value in coefficients.items()}
                rhs //= divisor_row
            signature = (tuple(coefficients[i] for i in ids), rhs)
            if signature in seen or sum(coefficients.values()) <= rhs:
                continue
            # Oversized optional strengthening must never make a valid base model overflow.
            if sum(abs(value) for value in coefficients.values()) + abs(rhs) >= 2**62:
                continue
            seen.add(signature)
            model.add(sum(coefficients[i] * selected[i] for i in ids) <= rhs)
            count += 1
    return count


def _pair_minimum_seconds(
    problem: RouteProblem,
    first: RoutableContract,
    second: RoutableContract,
) -> int | None:
    """Optimistic resource/deadline-feasible duration for exactly two contracts.

    Removing other actions and shortcutting travel cannot delay a pickup or increase the time
    between its pickup and delivery. Thus both absolute and rolling deadlines survive projection
    onto these four events. Required waypoints and active shipments are safely omitted here.
    """

    constraints = problem.constraints
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
        elapsed = 0
        pickup_times: dict[int, int] = {}
        cargo_load_units = 0
        active_contract_count = 0
        locked_collateral_units = 0
        is_feasible = True
        for event_index in event_order:
            contract = contracts[event_index // 2]
            is_pickup = event_index % 2 == 0
            if is_pickup:
                cargo_load_units += contract.volume_units
                active_contract_count += 1
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    locked_collateral_units += contract.collateral_units
                target_system_id = contract.origin_system_id
            else:
                cargo_load_units -= contract.volume_units
                active_contract_count -= 1
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    locked_collateral_units -= contract.collateral_units
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
            leg_jump_count = problem.jump_matrix.get((current_system_id, target_system_id))
            if leg_jump_count is None:
                is_feasible = False
                break
            arrival = elapsed + leg_jump_count * constraints.travel.seconds_per_jump
            elapsed = arrival + constraints.travel.service_seconds
            if is_pickup:
                pickup_times[event_index // 2] = arrival
                if (
                    constraints.collateral_mode is CollateralMode.ROLLING
                    and arrival > contract.last_pickup_second(constraints.snapshot_time)
                ):
                    is_feasible = False
                    break
            else:
                deadline = contract.days_to_complete * 86_400
                if constraints.collateral_mode is CollateralMode.ROLLING:
                    deadline += pickup_times[event_index // 2]
                if elapsed > deadline:
                    is_feasible = False
                    break
            current_system_id = target_system_id
        terminal_system_id = constraints.terminal_system_id
        if is_feasible and terminal_system_id is not None:
            finish_jump_count = problem.jump_matrix.get((current_system_id, terminal_system_id))
            if finish_jump_count is None:
                is_feasible = False
            else:
                elapsed += finish_jump_count * constraints.travel.seconds_per_jump
        if is_feasible:
            minimum_route_seconds = (
                elapsed if minimum_route_seconds is None else min(minimum_route_seconds, elapsed)
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


def build_selection_cuts(problem: RouteProblem) -> SelectionCuts:
    """Derive pair and clique incompatibilities from optimistic necessary conditions."""

    constraints = problem.constraints
    contract_ids = tuple(contract.contract_id for contract in problem.contracts)
    contract_by_id = {contract.contract_id: contract for contract in problem.contracts}
    active_collateral_units = problem.initial_collateral_units
    incompatible_contract_pairs: list[tuple[int, int]] = []
    for first_id, second_id in combinations(contract_ids, 2):
        collateral_conflict = False
        if constraints.collateral_mode is CollateralMode.LOCKED:
            collateral_conflict = (
                active_collateral_units
                + contract_by_id[first_id].collateral_units
                + contract_by_id[second_id].collateral_units
                > constraints.collateral_budget_units
            )
        minimum_seconds = _pair_minimum_seconds(
            problem,
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
