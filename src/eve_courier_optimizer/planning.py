"""Turn a raw contract snapshot into the smaller, auditable problem sent to the solver.

Preprocessing removes contracts only when they violate declared policy or can be proven useless
on their own. Those safe reductions preserve the global optimum. The optional candidate cap is
different: it is a speed heuristic, so using it is recorded in the problem scope and prevents a
claim of optimality over the full snapshot.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import timedelta
from math import inf

from .domain import (
    ActiveShipment,
    CollateralMode,
    PlanningConstraints,
    ProblemScope,
    RoutableContract,
    RouteProblem,
)
from .sde import UniverseGraph, build_jump_matrix
from .snapshot import ContractSnapshot


@dataclass(frozen=True, slots=True)
class SingleContractScore:
    contract: RoutableContract
    solo_jumps: int
    solo_seconds: int
    reward_per_hour_isk: float
    reward_per_jump_isk: float
    reward_to_collateral: float


@dataclass(frozen=True, slots=True)
class PreparedProblem:
    problem: RouteProblem
    jump_matrix: dict[tuple[int, int], int]
    scores: tuple[SingleContractScore, ...]


def _count_tuple(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counter.items()))


def _solo_seconds(
    jumps_to_pickup: int,
    delivery_jumps: int,
    constraints: PlanningConstraints,
) -> int:
    return (
        jumps_to_pickup + delivery_jumps
    ) * constraints.travel.seconds_per_jump + 2 * constraints.travel.service_seconds


def _score(
    contract: RoutableContract,
    solo_jumps: int,
    solo_seconds: int,
) -> SingleContractScore:
    reward_isk = contract.contract.reward_units / 100.0
    collateral_isk = contract.contract.collateral_units / 100.0
    return SingleContractScore(
        contract=contract,
        solo_jumps=solo_jumps,
        solo_seconds=solo_seconds,
        reward_per_hour_isk=(reward_isk * 3600.0 / solo_seconds if solo_seconds else inf),
        reward_per_jump_isk=(reward_isk / solo_jumps) if solo_jumps else inf,
        reward_to_collateral=(reward_isk / collateral_isk) if collateral_isk else inf,
    )


def _validate_threat_coverage(
    graph: UniverseGraph,
    constraints: PlanningConstraints,
) -> None:
    """Reject threat-aware proofs whose potentially traversable region envelope was not observed."""

    security = constraints.security
    if not security.threat_categories:
        return

    # Threat/activity avoids are observations that can change on the next refresh. Strip them when
    # deriving the coverage envelope so a region cannot disappear from required coverage merely
    # because yesterday's observation happened to block the only gate leading to it. Manual avoids
    # and the declared security bands are stable operator policy and therefore remain in force.
    coverage_policy = replace(
        security,
        gank_avoided_system_ids=frozenset(),
        gank_ship_kill_threshold=None,
        gank_activity_fetched_at=None,
        threat_avoided_system_ids=frozenset(),
        threat_categories=frozenset(),
        threat_min_events=None,
        threat_intel_fetched_at=None,
        threat_window_seconds=None,
        threat_gate_radius_m=None,
        threat_coverage_region_ids=frozenset(),
        threat_incomplete_region_ids=frozenset(),
    )
    max_jumps = constraints.horizon_seconds // constraints.travel.seconds_per_jump
    required = graph.reachable_region_ids(
        constraints.start_system_id,
        coverage_policy,
        max_jumps=max_jumps,
    )
    missing = required - security.threat_coverage_region_ids
    if not missing:
        return
    names = [
        graph.regions[region_id].name for region_id in sorted(missing) if region_id in graph.regions
    ]
    preview = ", ".join(names[:5])
    suffix = "…" if len(names) > 5 else ""
    raise ValueError(
        "gate-threat observation does not cover "
        f"{len(missing)} route-reachable region(s)"
        f"{f' ({preview}{suffix})' if preview else ''}; rescan threat intel for the current "
        "start, security bands, and time budget"
    )


def _validate_problem_context(
    snapshot: ContractSnapshot,
    graph: UniverseGraph,
    constraints: PlanningConstraints,
    max_candidates: int | None,
) -> None:
    """Validate snapshot compatibility and every system required by route policy."""

    if snapshot.sde_build_number != graph.metadata.build_number:
        raise ValueError(
            f"snapshot uses SDE {snapshot.sde_build_number}, loaded graph is "
            f"{graph.metadata.build_number}; refresh one of them before proving optimality"
        )
    if constraints.snapshot_time < snapshot.fetched_at:
        raise ValueError("planning time cannot predate the contract snapshot")
    if max_candidates is not None and max_candidates <= 0:
        raise ValueError("max_candidates must be positive when supplied")

    start_system = graph.systems.get(constraints.start_system_id)
    if start_system is None:
        raise ValueError(f"start system {constraints.start_system_id} is not present in the SDE")
    start_rejection_reason = constraints.security.rejection_reason(
        constraints.start_system_id,
        start_system.security_status,
    )
    if start_rejection_reason is not None:
        raise ValueError(f"start system is excluded by {start_rejection_reason.replace('_', ' ')}")

    required_route_system_ids = set(constraints.required_system_ids)
    if constraints.terminal_system_id is not None:
        required_route_system_ids.add(constraints.terminal_system_id)
    for system_id in sorted(required_route_system_ids):
        system = graph.systems.get(system_id)
        if system is None:
            raise ValueError(f"required route system {system_id} is not present in the SDE")
        rejection_reason = constraints.security.rejection_reason(system_id, system.security_status)
        if rejection_reason is not None:
            raise ValueError(
                f"required route system {system.name} is excluded by "
                f"{rejection_reason.replace('_', ' ')}"
            )

    _validate_threat_coverage(graph, constraints)


def _validate_active_shipments(
    active_shipments: tuple[ActiveShipment, ...],
    constraints: PlanningConstraints,
) -> tuple[set[int], int]:
    """Return active IDs and their initial cargo/collateral after validating limits."""

    active_contract_ids = {shipment.contract.contract.contract_id for shipment in active_shipments}
    if len(active_contract_ids) != len(active_shipments):
        raise ValueError("active shipment contract IDs must be unique")

    picked_shipment_count = sum(1 for shipment in active_shipments if shipment.picked)
    if (
        constraints.max_simultaneous_contracts is not None
        and picked_shipment_count > constraints.max_simultaneous_contracts
    ):
        raise ValueError("active shipments already exceed the simultaneous-contract limit")

    initial_cargo_load_units = sum(
        shipment.contract.contract.volume_units for shipment in active_shipments if shipment.picked
    )
    initial_locked_collateral_units = sum(
        shipment.contract.contract.collateral_units for shipment in active_shipments
    )
    if initial_cargo_load_units > constraints.cargo_capacity_units:
        raise ValueError("active shipments already exceed cargo capacity")
    if initial_locked_collateral_units > constraints.collateral_budget_units:
        raise ValueError("active shipments already exceed collateral budget")

    return active_contract_ids, initial_locked_collateral_units


def _resolve_routable_contracts(
    snapshot: ContractSnapshot,
    graph: UniverseGraph,
    constraints: PlanningConstraints,
    active_contract_ids: set[int],
    excluded_contract_ids: frozenset[int],
    policy_exclusions: Counter[str],
    safe_reductions: Counter[str],
) -> list[RoutableContract]:
    """Resolve station endpoints to systems and apply declared security policy."""

    routable_contracts: list[RoutableContract] = []
    for public_contract in snapshot.contracts:
        if public_contract.contract_id in excluded_contract_ids:
            # A completed courier may linger in ESI briefly. Within one execution session the
            # same contract cannot be accepted a second time, so this is a safe reduction.
            safe_reductions["completed_in_session"] += 1
            continue
        if public_contract.contract_id in active_contract_ids:
            # A just-accepted job may remain visible in the public snapshot while ESI caches
            # update. Its active-shipment copy is mandatory and already earns the reward once.
            safe_reductions["already_active_commitment"] += 1
            continue

        origin_system_id = graph.station_system(public_contract.origin_location_id)
        destination_system_id = graph.station_system(public_contract.destination_location_id)
        if origin_system_id is None or destination_system_id is None:
            policy_exclusions["unsupported_non_npc_station_endpoint"] += 1
            continue

        origin_system = graph.systems[origin_system_id]
        destination_system = graph.systems[destination_system_id]
        rejection_reason = constraints.security.rejection_reason(
            origin_system_id, origin_system.security_status
        )
        if rejection_reason is None:
            rejection_reason = constraints.security.rejection_reason(
                destination_system_id,
                destination_system.security_status,
            )
        if rejection_reason is not None:
            policy_exclusions[rejection_reason] += 1
            continue

        routable_contracts.append(
            RoutableContract(
                contract=public_contract,
                origin_system_id=origin_system_id,
                destination_system_id=destination_system_id,
            )
        )
    return routable_contracts


def _build_solo_jump_lower_bounds(
    routable_contracts: list[RoutableContract],
    graph: UniverseGraph,
    constraints: PlanningConstraints,
) -> tuple[dict[int, int], dict[tuple[int, int], int]]:
    """Find minimum jumps to each pickup and from each pickup to its delivery.

    These sparse distances are enough to prove that some contracts cannot work even when they
    are the only optional contract in the route. The full all-pairs matrix is built later, after
    those contracts have been removed.
    """

    pickup_system_ids = {contract.origin_system_id for contract in routable_contracts}
    jumps_from_start_by_pickup_system = graph.distances_from(
        constraints.start_system_id,
        pickup_system_ids,
        constraints.security,
    )

    delivery_system_ids_by_pickup_system: dict[int, set[int]] = {}
    for contract in routable_contracts:
        delivery_system_ids_by_pickup_system.setdefault(contract.origin_system_id, set()).add(
            contract.destination_system_id
        )

    delivery_jumps_by_system_pair: dict[tuple[int, int], int] = {}
    for (
        pickup_system_id,
        delivery_system_ids,
    ) in delivery_system_ids_by_pickup_system.items():
        jumps_by_delivery_system = graph.distances_from(
            pickup_system_id,
            delivery_system_ids,
            constraints.security,
        )
        for delivery_system_id, jump_count in jumps_by_delivery_system.items():
            delivery_jumps_by_system_pair[(pickup_system_id, delivery_system_id)] = jump_count

    return jumps_from_start_by_pickup_system, delivery_jumps_by_system_pair


def _filter_individually_feasible_contracts(
    routable_contracts: list[RoutableContract],
    constraints: PlanningConstraints,
    initial_locked_collateral_units: int,
    jumps_from_start_by_pickup_system: dict[int, int],
    delivery_jumps_by_system_pair: dict[tuple[int, int], int],
    safe_reductions: Counter[str],
) -> tuple[list[RoutableContract], list[SingleContractScore]]:
    """Remove contracts that are impossible even in an otherwise empty route."""

    eligible_contracts: list[RoutableContract] = []
    contract_scores: list[SingleContractScore] = []
    for routable_contract in routable_contracts:
        public_contract = routable_contract.contract
        if public_contract.date_expired <= constraints.snapshot_time:
            safe_reductions["listing_expired"] += 1
            continue
        if public_contract.reward_units <= 0:
            safe_reductions["nonpositive_reward"] += 1
            continue
        if public_contract.volume_units > constraints.cargo_capacity_units:
            safe_reductions["volume_exceeds_capacity"] += 1
            continue
        if public_contract.collateral_units > constraints.collateral_budget_units:
            safe_reductions["collateral_exceeds_budget"] += 1
            continue
        if constraints.max_simultaneous_contracts == 0:
            safe_reductions["simultaneous_contract_limit_zero"] += 1
            continue
        if (
            constraints.collateral_mode is CollateralMode.LOCKED
            and initial_locked_collateral_units + public_contract.collateral_units
            > constraints.collateral_budget_units
        ):
            safe_reductions["collateral_unavailable_at_start"] += 1
            continue

        jumps_to_pickup = jumps_from_start_by_pickup_system.get(routable_contract.origin_system_id)
        delivery_jumps = delivery_jumps_by_system_pair.get(
            (
                routable_contract.origin_system_id,
                routable_contract.destination_system_id,
            )
        )
        if jumps_to_pickup is None or delivery_jumps is None:
            safe_reductions["unreachable"] += 1
            continue

        solo_seconds = _solo_seconds(jumps_to_pickup, delivery_jumps, constraints)
        if solo_seconds > constraints.horizon_seconds:
            safe_reductions["solo_lower_bound_exceeds_horizon"] += 1
            continue

        delivery_window_seconds = public_contract.days_to_complete * 86_400
        if constraints.collateral_mode is CollateralMode.LOCKED:
            if solo_seconds > delivery_window_seconds:
                safe_reductions["solo_lower_bound_misses_delivery_deadline"] += 1
                continue
        else:
            earliest_pickup_seconds = jumps_to_pickup * constraints.travel.seconds_per_jump
            earliest_pickup_time = constraints.snapshot_time + timedelta(
                seconds=earliest_pickup_seconds
            )
            if earliest_pickup_time >= public_contract.date_expired:
                safe_reductions["solo_lower_bound_misses_listing_expiry"] += 1
                continue
            minimum_delivery_seconds = (
                delivery_jumps * constraints.travel.seconds_per_jump
                + 2 * constraints.travel.service_seconds
            )
            if minimum_delivery_seconds > delivery_window_seconds:
                safe_reductions["minimum_delivery_time_misses_deadline"] += 1
                continue

        eligible_contracts.append(routable_contract)
        contract_scores.append(
            _score(
                routable_contract,
                jumps_to_pickup + delivery_jumps,
                solo_seconds,
            )
        )

    return eligible_contracts, contract_scores


def _apply_candidate_cap(
    eligible_contracts: list[RoutableContract],
    contract_scores: list[SingleContractScore],
    max_candidates: int | None,
    heuristic_reductions: Counter[str],
) -> tuple[list[RoutableContract], list[SingleContractScore]]:
    """Optionally keep the best solo-scoring candidates and record the truncation."""

    if max_candidates is None or len(eligible_contracts) <= max_candidates:
        return eligible_contracts, contract_scores

    ranked_contracts = sorted(
        zip(eligible_contracts, contract_scores, strict=True),
        key=lambda contract_and_score: (
            contract_and_score[1].reward_per_hour_isk,
            contract_and_score[0].contract.reward_units,
            -contract_and_score[0].contract.contract_id,
        ),
        reverse=True,
    )
    kept_contracts = ranked_contracts[:max_candidates]
    heuristic_reductions["candidate_cap"] = len(eligible_contracts) - max_candidates
    return (
        [ranked_contract[0] for ranked_contract in kept_contracts],
        [ranked_contract[1] for ranked_contract in kept_contracts],
    )


def prepare_problem(
    snapshot: ContractSnapshot,
    graph: UniverseGraph,
    constraints: PlanningConstraints,
    *,
    active_shipments: tuple[ActiveShipment, ...] = (),
    excluded_contract_ids: frozenset[int] = frozenset(),
    max_candidates: int | None = None,
) -> PreparedProblem:
    """Apply declared policy exclusions and only mathematically safe reductions by default.

    ``max_candidates`` is deliberately opt-in. If used, the resulting certificate is marked as
    truncated and therefore cannot claim global optimality over the full eligible snapshot.
    """

    _validate_problem_context(snapshot, graph, constraints, max_candidates)
    active_contract_ids, initial_locked_collateral_units = _validate_active_shipments(
        active_shipments, constraints
    )

    # These counters become part of the proof scope, making every removed contract auditable.
    policy_exclusions: Counter[str] = Counter()
    safe_reductions: Counter[str] = Counter()
    routable_contracts = _resolve_routable_contracts(
        snapshot,
        graph,
        constraints,
        active_contract_ids,
        excluded_contract_ids,
        policy_exclusions,
        safe_reductions,
    )
    (
        jumps_from_start_by_pickup_system,
        delivery_jumps_by_system_pair,
    ) = _build_solo_jump_lower_bounds(routable_contracts, graph, constraints)
    eligible_contracts, contract_scores = _filter_individually_feasible_contracts(
        routable_contracts,
        constraints,
        initial_locked_collateral_units,
        jumps_from_start_by_pickup_system,
        delivery_jumps_by_system_pair,
        safe_reductions,
    )

    heuristic_reductions: Counter[str] = Counter()
    eligible_contracts, contract_scores = _apply_candidate_cap(
        eligible_contracts,
        contract_scores,
        max_candidates,
        heuristic_reductions,
    )

    # Only systems that survived preprocessing belong in the solver's exact distance matrix.
    relevant_system_ids = {constraints.start_system_id}
    relevant_system_ids.update(constraints.required_system_ids)
    if constraints.terminal_system_id is not None:
        relevant_system_ids.add(constraints.terminal_system_id)
    for contract in eligible_contracts:
        relevant_system_ids.update((contract.origin_system_id, contract.destination_system_id))
    for shipment in active_shipments:
        if not shipment.picked:
            relevant_system_ids.add(shipment.contract.origin_system_id)
        relevant_system_ids.add(shipment.contract.destination_system_id)
    jump_matrix = build_jump_matrix(graph, relevant_system_ids, constraints.security)

    scope = ProblemScope(
        snapshot_fetched_at=snapshot.fetched_at,
        snapshot_compatibility_date=snapshot.compatibility_date,
        sde_build_number=snapshot.sde_build_number,
        scanned_region_ids=snapshot.region_ids,
        public_couriers_seen=len(snapshot.contracts),
        eligible_contracts=len(eligible_contracts),
        policy_exclusions=_count_tuple(policy_exclusions),
        safe_reductions=_count_tuple(safe_reductions),
        heuristic_reductions=_count_tuple(heuristic_reductions),
    )
    problem = RouteProblem(
        constraints=constraints,
        contracts=tuple(eligible_contracts),
        scope=scope,
        active_shipments=active_shipments,
    )
    return PreparedProblem(
        problem=problem,
        jump_matrix=jump_matrix,
        scores=tuple(contract_scores),
    )


def rank_single_contracts(prepared: PreparedProblem) -> tuple[SingleContractScore, ...]:
    return tuple(
        sorted(
            prepared.scores,
            key=lambda score: (
                score.reward_per_hour_isk,
                score.contract.contract.reward_units,
            ),
            reverse=True,
        )
    )
