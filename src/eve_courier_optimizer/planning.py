"""Iterations 2/3: proof-preserving preprocessing and single-contract scoring."""

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
        (jumps_to_pickup + delivery_jumps) * constraints.travel.seconds_per_jump
        + 2 * constraints.travel.service_seconds
    )


def _score(contract: RoutableContract, jumps: int, seconds: int) -> SingleContractScore:
    reward_isk = contract.contract.reward_units / 100.0
    collateral_isk = contract.contract.collateral_units / 100.0
    return SingleContractScore(
        contract=contract,
        solo_jumps=jumps,
        solo_seconds=seconds,
        reward_per_hour_isk=(reward_isk * 3600.0 / seconds) if seconds else inf,
        reward_per_jump_isk=(reward_isk / jumps) if jumps else inf,
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
        graph.regions[region_id].name
        for region_id in sorted(missing)
        if region_id in graph.regions
    ]
    preview = ", ".join(names[:5])
    suffix = "…" if len(names) > 5 else ""
    raise ValueError(
        "gate-threat observation does not cover "
        f"{len(missing)} route-reachable region(s)"
        f"{f' ({preview}{suffix})' if preview else ''}; rescan threat intel for the current "
        "start, security bands, and time budget"
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

    if snapshot.sde_build_number != graph.metadata.build_number:
        raise ValueError(
            f"snapshot uses SDE {snapshot.sde_build_number}, loaded graph is "
            f"{graph.metadata.build_number}; refresh one of them before proving optimality"
        )
    if constraints.snapshot_time < snapshot.fetched_at:
        raise ValueError("planning time cannot predate the contract snapshot")
    if max_candidates is not None and max_candidates <= 0:
        raise ValueError("max_candidates must be positive when supplied")
    if constraints.start_system_id not in graph.systems:
        raise ValueError(f"start system {constraints.start_system_id} is not present in the SDE")
    start = graph.systems[constraints.start_system_id]
    start_rejection = constraints.security.rejection_reason(
        constraints.start_system_id,
        start.security_status,
    )
    if start_rejection is not None:
        raise ValueError(f"start system is excluded by {start_rejection.replace('_', ' ')}")
    required_route_systems = set(constraints.required_system_ids)
    terminal_system_id = constraints.terminal_system_id
    if terminal_system_id is not None:
        required_route_systems.add(terminal_system_id)
    for system_id in sorted(required_route_systems):
        system = graph.systems.get(system_id)
        if system is None:
            raise ValueError(f"required route system {system_id} is not present in the SDE")
        rejection = constraints.security.rejection_reason(system_id, system.security_status)
        if rejection is not None:
            raise ValueError(
                f"required route system {system.name} is excluded by "
                f"{rejection.replace('_', ' ')}"
            )
    _validate_threat_coverage(graph, constraints)

    active_ids = [
        shipment.contract.contract.contract_id for shipment in active_shipments
    ]
    if len(active_ids) != len(set(active_ids)):
        raise ValueError("active shipment contract IDs must be unique")
    picked_active = sum(1 for shipment in active_shipments if shipment.picked)
    if (
        constraints.max_simultaneous_contracts is not None
        and picked_active > constraints.max_simultaneous_contracts
    ):
        raise ValueError("active shipments already exceed the simultaneous-contract limit")

    policy_exclusions: Counter[str] = Counter()
    safe_reductions: Counter[str] = Counter()
    resolved: list[RoutableContract] = []
    active_contract_ids = set(active_ids)

    for public in snapshot.contracts:
        if public.contract_id in excluded_contract_ids:
            # A completed courier may linger in ESI briefly. Within one execution session the
            # same contract cannot be accepted a second time, so this is a safe reduction.
            safe_reductions["completed_in_session"] += 1
            continue
        if public.contract_id in active_contract_ids:
            # ESI/cache propagation can leave a just-accepted job visible in a public observation.
            # Its active-shipment copy is mandatory and already contributes reward exactly once.
            safe_reductions["already_active_commitment"] += 1
            continue
        origin = graph.station_system(public.origin_location_id)
        destination = graph.station_system(public.destination_location_id)
        if origin is None or destination is None:
            policy_exclusions["unsupported_non_npc_station_endpoint"] += 1
            continue
        origin_system = graph.systems[origin]
        destination_system = graph.systems[destination]
        rejection = constraints.security.rejection_reason(origin, origin_system.security_status)
        if rejection is None:
            rejection = constraints.security.rejection_reason(
                destination,
                destination_system.security_status,
            )
        if rejection is not None:
            policy_exclusions[rejection] += 1
            continue
        routable = RoutableContract(public, origin, destination)
        resolved.append(routable)

    # Eligibility needs only start->pickup and pickup->delivery lower bounds. Building a full
    # all-pairs closure before filtering performs many unnecessary BFS traversals and bloats the
    # proof input with systems that are later removed. Compute the sparse lower bounds first, then
    # build the exact closure only for retained events below.
    origins = {item.origin_system_id for item in resolved}
    from_start = graph.distances_from(
        constraints.start_system_id,
        origins,
        constraints.security,
    )
    destinations_by_origin: dict[int, set[int]] = {}
    for item in resolved:
        destinations_by_origin.setdefault(item.origin_system_id, set()).add(
            item.destination_system_id
        )
    delivery_distances: dict[tuple[int, int], int] = {}
    for origin, destinations in destinations_by_origin.items():
        for destination, jumps in graph.distances_from(
            origin,
            destinations,
            constraints.security,
        ).items():
            delivery_distances[(origin, destination)] = jumps

    initial_volume = sum(
        s.contract.contract.volume_units for s in active_shipments if s.picked
    )
    initial_collateral = sum(s.contract.contract.collateral_units for s in active_shipments)
    if initial_volume > constraints.cargo_capacity_units:
        raise ValueError("active shipments already exceed cargo capacity")
    if initial_collateral > constraints.collateral_budget_units:
        raise ValueError("active shipments already exceed collateral budget")

    eligible: list[RoutableContract] = []
    scores: list[SingleContractScore] = []
    for contract in resolved:
        public = contract.contract
        if public.date_expired <= constraints.snapshot_time:
            safe_reductions["listing_expired"] += 1
            continue
        if public.reward_units <= 0:
            safe_reductions["nonpositive_reward"] += 1
            continue
        if public.volume_units > constraints.cargo_capacity_units:
            safe_reductions["volume_exceeds_capacity"] += 1
            continue
        if public.collateral_units > constraints.collateral_budget_units:
            safe_reductions["collateral_exceeds_budget"] += 1
            continue
        if constraints.max_simultaneous_contracts == 0:
            safe_reductions["simultaneous_contract_limit_zero"] += 1
            continue
        if (
            constraints.collateral_mode is CollateralMode.LOCKED
            and initial_collateral + public.collateral_units > constraints.collateral_budget_units
        ):
            safe_reductions["collateral_unavailable_at_start"] += 1
            continue
        to_pickup = from_start.get(contract.origin_system_id)
        delivery = delivery_distances.get(
            (contract.origin_system_id, contract.destination_system_id)
        )
        if to_pickup is None or delivery is None:
            safe_reductions["unreachable"] += 1
            continue
        solo_seconds = _solo_seconds(to_pickup, delivery, constraints)
        if solo_seconds > constraints.horizon_seconds:
            safe_reductions["solo_lower_bound_exceeds_horizon"] += 1
            continue
        completion_window = public.days_to_complete * 86_400
        if constraints.collateral_mode is CollateralMode.LOCKED:
            if solo_seconds > completion_window:
                safe_reductions["solo_lower_bound_misses_delivery_deadline"] += 1
                continue
        else:
            earliest_pickup = to_pickup * constraints.travel.seconds_per_jump
            earliest_pickup_at = constraints.snapshot_time + timedelta(seconds=earliest_pickup)
            if earliest_pickup_at >= public.date_expired:
                safe_reductions["solo_lower_bound_misses_listing_expiry"] += 1
                continue
            minimum_after_acceptance = (
                delivery * constraints.travel.seconds_per_jump
                + 2 * constraints.travel.service_seconds
            )
            if minimum_after_acceptance > completion_window:
                safe_reductions["minimum_delivery_time_misses_deadline"] += 1
                continue
        eligible.append(contract)
        scores.append(_score(contract, to_pickup + delivery, solo_seconds))

    heuristic_reductions: Counter[str] = Counter()
    if max_candidates is not None and len(eligible) > max_candidates:
        ranked = sorted(
            zip(eligible, scores, strict=True),
            key=lambda item: (
                item[1].reward_per_hour_isk,
                item[0].contract.reward_units,
                -item[0].contract.contract_id,
            ),
            reverse=True,
        )
        kept = ranked[:max_candidates]
        heuristic_reductions["candidate_cap"] = len(eligible) - max_candidates
        eligible = [item[0] for item in kept]
        scores = [item[1] for item in kept]

    relevant_systems = {constraints.start_system_id}
    relevant_systems.update(constraints.required_system_ids)
    if terminal_system_id is not None:
        relevant_systems.add(terminal_system_id)
    for contract in eligible:
        relevant_systems.update((contract.origin_system_id, contract.destination_system_id))
    for shipment in active_shipments:
        if not shipment.picked:
            relevant_systems.add(shipment.contract.origin_system_id)
        relevant_systems.add(shipment.contract.destination_system_id)
    jump_matrix = build_jump_matrix(graph, relevant_systems, constraints.security)

    scope = ProblemScope(
        snapshot_fetched_at=snapshot.fetched_at,
        snapshot_compatibility_date=snapshot.compatibility_date,
        sde_build_number=snapshot.sde_build_number,
        scanned_region_ids=snapshot.region_ids,
        public_couriers_seen=len(snapshot.contracts),
        eligible_contracts=len(eligible),
        policy_exclusions=_count_tuple(policy_exclusions),
        safe_reductions=_count_tuple(safe_reductions),
        heuristic_reductions=_count_tuple(heuristic_reductions),
    )
    problem = RouteProblem(
        constraints=constraints,
        contracts=tuple(eligible),
        scope=scope,
        active_shipments=active_shipments,
    )
    return PreparedProblem(problem=problem, jump_matrix=jump_matrix, scores=tuple(scores))


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
