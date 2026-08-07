"""Command-line interface for scanning, proving routes, and live replanning."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from .domain import (
    CollateralMode,
    PlanningConstraints,
    ProofStatus,
    SecurityBand,
    SecurityPolicy,
    ThreatCategory,
    TravelTimeModel,
    cargo_capacity_to_units,
    isk_to_units,
    isk_units_to_decimal,
    parse_human_isk,
)
from .esi import EsiClient, EsiResponseCache, default_cache_path
from .execution import (
    constraints_for_replan,
    initial_execution_state,
    read_execution_state,
    record_delivery,
    record_pickup,
    record_route_system,
    write_execution_state,
)
from .planning import prepare_problem, rank_single_contracts
from .reporting import write_solve_result
from .scanner import (
    DEFAULT_CONTRACT_SCAN_WORKERS,
    MAX_CONTRACT_SCAN_WORKERS,
    scan_public_couriers,
)
from .sde import UniverseGraph, load_bundled_graph
from .snapshot import ContractSnapshot, read_snapshot, write_snapshot
from .solver import SolverConfig, solve_exact
from .threat_intel import (
    DEFAULT_THREAT_WINDOW_SECONDS,
    ZkillClient,
    default_zkill_cache_path,
    threat_avoided_systems,
)
from .webapp import default_web_workspace, run_local_web_ui


def _positive_decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive finite number")
    return parsed


def _nonnegative_decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative finite number")
    return parsed


def _human_isk(value: str) -> Decimal:
    try:
        parsed = parse_human_isk(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _hours_to_seconds(value: Decimal) -> int:
    return int((value * 3600).to_integral_value(rounding=ROUND_FLOOR))


def _parse_time(value: str) -> datetime:
    if value.lower() == "now":
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _resolve_system(graph: UniverseGraph, value: str) -> int:
    try:
        system_id = int(value)
    except ValueError:
        matches = [
            system.system_id
            for system in graph.systems.values()
            if system.name.casefold() == value.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(f"could not resolve unique system {value!r}") from None
        return matches[0]
    if system_id not in graph.systems:
        raise ValueError(f"unknown system ID {system_id}")
    return system_id


def _resolve_region(graph: UniverseGraph, value: str) -> int:
    try:
        region_id = int(value)
    except ValueError:
        matches = [
            region.region_id
            for region in graph.regions.values()
            if region.name.casefold() == value.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(f"could not resolve unique region {value!r}") from None
        return matches[0]
    if region_id not in graph.regions:
        raise ValueError(f"unknown region ID {region_id}")
    return region_id


def _security_policy(
    graph: UniverseGraph,
    arguments: argparse.Namespace,
    snapshot: ContractSnapshot,
) -> SecurityPolicy:
    combinations = {
        "highsec": frozenset({SecurityBand.HIGH}),
        "lowsec": frozenset({SecurityBand.LOW}),
        "nullsec": frozenset({SecurityBand.NULL}),
        "high+low": frozenset({SecurityBand.HIGH, SecurityBand.LOW}),
        "high+null": frozenset({SecurityBand.HIGH, SecurityBand.NULL}),
        "low+null": frozenset({SecurityBand.LOW, SecurityBand.NULL}),
        "any": frozenset(SecurityBand),
    }
    avoided = frozenset(_resolve_system(graph, item) for item in arguments.avoid_system)
    threshold = arguments.gank_ship_kill_threshold
    activity_time = None
    gank_avoids: frozenset[int] = frozenset()
    start_system_id = _resolve_system(graph, arguments.start)
    if threshold is not None:
        if snapshot.system_kills_fetched_at is None:
            raise ValueError(
                "gank awareness needs system-kill activity; create a fresh snapshot with scan"
            )
        gank_avoids = frozenset(
            item.system_id
            for item in snapshot.system_kill_activity
            if item.ship_kills >= threshold and item.system_id != start_system_id
        )
        activity_time = snapshot.system_kills_fetched_at
    threat_categories = frozenset(ThreatCategory(item) for item in arguments.avoid_threat)
    threat_avoids: frozenset[int] = frozenset()
    if threat_categories:
        if snapshot.threat_intel_fetched_at is None:
            raise ValueError(
                "gate-threat awareness needs zKill intel; scan with --threat-intel first"
            )
        threat_avoids = threat_avoided_systems(
            snapshot.gate_threat_events,
            threat_categories,
            minimum_events=arguments.threat_min_events,
            exempt_system_ids=frozenset({start_system_id}),
        )
    return SecurityPolicy(
        minimum_security=None,
        avoided_system_ids=avoided,
        allowed_bands=combinations[arguments.security],
        gank_avoided_system_ids=gank_avoids,
        gank_ship_kill_threshold=threshold,
        gank_activity_fetched_at=activity_time,
        threat_avoided_system_ids=threat_avoids,
        threat_categories=threat_categories,
        threat_min_events=arguments.threat_min_events if threat_categories else None,
        threat_intel_fetched_at=(
            snapshot.threat_intel_fetched_at if threat_categories else None
        ),
        threat_window_seconds=snapshot.threat_window_seconds if threat_categories else None,
        threat_gate_radius_m=snapshot.threat_gate_radius_m if threat_categories else None,
        threat_coverage_region_ids=(
            frozenset(snapshot.threat_coverage_region_ids)
            if threat_categories
            else frozenset()
        ),
        threat_incomplete_region_ids=(
            frozenset(snapshot.threat_incomplete_region_ids)
            if threat_categories
            else frozenset()
        ),
    )


def _constraints(
    graph: UniverseGraph,
    arguments: argparse.Namespace,
    snapshot: ContractSnapshot,
) -> PlanningConstraints:
    return PlanningConstraints(
        start_system_id=_resolve_system(graph, arguments.start),
        cargo_capacity_units=cargo_capacity_to_units(arguments.cargo_m3),
        collateral_budget_units=isk_to_units(arguments.collateral_isk),
        horizon_seconds=_hours_to_seconds(arguments.hours),
        snapshot_time=snapshot.fetched_at,
        collateral_mode=CollateralMode(arguments.collateral_mode),
        travel=TravelTimeModel(
            seconds_per_jump=arguments.seconds_per_jump,
            service_seconds=arguments.service_seconds,
        ),
        security=_security_policy(graph, arguments, snapshot),
        return_to_start=arguments.loop,
        required_system_ids=frozenset(
            _resolve_system(graph, value) for value in arguments.require_system
        ),
        finish_system_id=(
            _resolve_system(graph, arguments.finish) if arguments.finish is not None else None
        ),
        max_simultaneous_contracts=arguments.max_simultaneous_contracts,
    )


def _solver_config(arguments: argparse.Namespace) -> SolverConfig:
    return SolverConfig(
        max_time_seconds=arguments.time_limit,
        num_workers=arguments.workers,
        log_search_progress=arguments.solver_log,
    )


def _add_planning_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", required=True, help="start solar-system ID or exact name")
    parser.add_argument("--cargo-m3", type=_nonnegative_decimal, required=True)
    parser.add_argument(
        "--collateral-isk",
        type=_human_isk,
        required=True,
        help="collateral limit in ISK; K/M/B suffixes are accepted (for example 1.5B)",
    )
    parser.add_argument("--hours", type=_positive_decimal, required=True)
    parser.add_argument(
        "--collateral-mode",
        choices=[mode.value for mode in CollateralMode],
        default=CollateralMode.LOCKED.value,
    )
    parser.add_argument(
        "--avoid-threat",
        action="append",
        choices=[category.value for category in ThreatCategory],
        default=[],
        help="avoid systems matching this gate-focused zKill category (repeatable)",
    )
    parser.add_argument(
        "--threat-min-events",
        type=_positive_int,
        default=1,
        help="matching gate events required before a system is avoided (default: 1)",
    )
    parser.add_argument(
        "--security",
        choices=["highsec", "lowsec", "nullsec", "high+low", "high+null", "low+null", "any"],
        default="highsec",
    )
    parser.add_argument("--avoid-system", action="append", default=[])
    parser.add_argument(
        "--require-system",
        action="append",
        default=[],
        help="system ID or exact name that the route must visit; repeat as needed",
    )
    parser.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="require the route to return to its start (default: enabled)",
    )
    parser.add_argument(
        "--finish",
        help="required final system ID or exact name; use with --no-loop",
    )
    parser.add_argument(
        "--max-simultaneous-contracts",
        type=_nonnegative_int,
        help="maximum picked-but-undelivered courier contracts at once",
    )
    parser.add_argument(
        "--gank-ship-kill-threshold",
        type=_positive_int,
        help=(
            "optional activity proxy: avoid systems at/above this ESI ship-kill count; "
            "aggregate kills are not a suicide-gank classification"
        ),
    )
    parser.add_argument("--seconds-per-jump", type=int, default=60)
    parser.add_argument("--service-seconds", type=int, default=30)
    parser.add_argument(
        "--max-candidates",
        type=int,
        help="heuristic cap; using it makes the global proof scope explicitly truncated",
    )


def _add_solver_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--solver-log", action="store_true")
    parser.add_argument("--require-optimal", action="store_true")
    parser.add_argument("--require-global-optimal", action="store_true")


def _run_scan(arguments: argparse.Namespace, graph: UniverseGraph) -> int:
    region_ids = tuple(_resolve_region(graph, value) for value in arguments.region)
    threat_region_ids = (
        tuple(_resolve_region(graph, value) for value in arguments.threat_region)
        if arguments.threat_region
        else None
    )
    cache = EsiResponseCache(arguments.cache)
    client = EsiClient(cache=cache)
    zkill = ZkillClient(cache=EsiResponseCache(arguments.zkill_cache))
    snapshot = scan_public_couriers(
        client,
        graph,
        region_ids,
        include_system_kills=True,
        zkill=zkill,
        include_threat_intel=arguments.threat_intel,
        threat_window_seconds=arguments.threat_window_hours * 3_600,
        threat_gate_radius_m=arguments.gate_radius_km * 1_000,
        threat_region_ids=threat_region_ids,
        contract_workers=arguments.scan_workers,
    )
    write_snapshot(arguments.output, snapshot)
    print(
        f"snapshot: {len(snapshot.contracts)} public couriers across {len(snapshot.region_ids)} "
        f"region(s), SDE {snapshot.sde_build_number} -> {arguments.output}"
    )
    return 0


def _run_rank(arguments: argparse.Namespace, graph: UniverseGraph) -> int:
    snapshot = read_snapshot(arguments.snapshot)
    constraints = _constraints(graph, arguments, snapshot)
    prepared = prepare_problem(
        snapshot,
        graph,
        constraints,
        max_candidates=arguments.max_candidates,
    )
    print("contract_id reward_ISK solo_jumps solo_minutes ISK/hour reward/collateral")
    for score in rank_single_contracts(prepared)[: arguments.limit]:
        contract = score.contract.contract
        print(
            f"{contract.contract_id} "
            f"{isk_units_to_decimal(contract.reward_units)} "
            f"{score.solo_jumps} {score.solo_seconds / 60:.1f} "
            f"{score.reward_per_hour_isk:.0f} {score.reward_to_collateral:.5f}"
        )
    print(
        f"eligible={prepared.problem.scope.eligible_contracts} "
        f"policy_exclusions={dict(prepared.problem.scope.policy_exclusions)} "
        f"safe_reductions={dict(prepared.problem.scope.safe_reductions)}"
    )
    return 0


def _proof_exit(arguments: argparse.Namespace, status: ProofStatus, untruncated: bool) -> int:
    if arguments.require_global_optimal and not (
        status is ProofStatus.PROVEN_OPTIMAL and untruncated
    ):
        return 3
    if arguments.require_optimal and status is not ProofStatus.PROVEN_OPTIMAL:
        return 2
    return 0


def _print_solve_summary(result_path: Path, result_status: ProofStatus, result: object) -> None:
    del result  # Kept as a small boundary for future richer terminal rendering.
    print(f"plan: {result_status.value} -> {result_path}")


def _run_solve(arguments: argparse.Namespace, graph: UniverseGraph) -> int:
    snapshot = read_snapshot(arguments.snapshot)
    constraints = _constraints(graph, arguments, snapshot)
    prepared = prepare_problem(
        snapshot,
        graph,
        constraints,
        max_candidates=arguments.max_candidates,
    )
    result = solve_exact(prepared, graph, config=_solver_config(arguments))
    write_solve_result(arguments.output, result, prepared.problem)
    if arguments.state_output is not None and result.certificate.feasibility_verified:
        state = initial_execution_state(
            prepared.problem.constraints,
            prepared.problem.contracts,
            prepared.problem.active_shipments,
            result,
        )
        write_execution_state(arguments.state_output, state)
    _print_solve_summary(arguments.output, result.certificate.status, result)
    if result.certificate.objective_units is not None:
        print(
            f"reward={isk_units_to_decimal(result.certificate.objective_units)} ISK "
            f"bound={isk_units_to_decimal(result.certificate.best_bound_units or 0)} ISK "
            f"gap={result.certificate.relative_gap or 0:.6%} "
            f"scope_untruncated={result.certificate.scope_untruncated}"
        )
    return _proof_exit(
        arguments,
        result.certificate.status,
        result.certificate.scope_untruncated,
    )


def _run_replan(arguments: argparse.Namespace, graph: UniverseGraph) -> int:
    snapshot = read_snapshot(arguments.snapshot)
    state = read_execution_state(arguments.state)
    constraints = constraints_for_replan(state, snapshot)
    prepared = prepare_problem(
        snapshot,
        graph,
        constraints,
        active_shipments=state.active_shipments,
        excluded_contract_ids=frozenset(state.completed_contract_ids),
        max_candidates=arguments.max_candidates,
    )
    result = solve_exact(prepared, graph, config=_solver_config(arguments))
    write_solve_result(arguments.output, result, prepared.problem)
    if arguments.state_output is not None and result.certificate.feasibility_verified:
        next_state = initial_execution_state(
            prepared.problem.constraints,
            prepared.problem.contracts,
            prepared.problem.active_shipments,
            result,
            completed_contract_ids=state.completed_contract_ids,
        )
        write_execution_state(arguments.state_output, next_state)
    _print_solve_summary(arguments.output, result.certificate.status, result)
    return _proof_exit(
        arguments,
        result.certificate.status,
        result.certificate.scope_untruncated,
    )


def _run_advance(arguments: argparse.Namespace, graph: UniverseGraph) -> int:
    state = read_execution_state(arguments.state)
    at = _parse_time(arguments.at)
    if arguments.action == "pickup":
        if arguments.contract_id is None or arguments.snapshot is None:
            raise ValueError("pickup requires --contract-id and --snapshot")
        snapshot = read_snapshot(arguments.snapshot)
        updated = record_pickup(state, snapshot, graph, arguments.contract_id, at)
        subject = str(arguments.contract_id)
    elif arguments.action == "delivery":
        if arguments.contract_id is None:
            raise ValueError("delivery requires --contract-id")
        updated = record_delivery(state, arguments.contract_id, at)
        subject = str(arguments.contract_id)
    else:
        if arguments.system is None:
            raise ValueError("route-system requires --system")
        system_id = _resolve_system(graph, arguments.system)
        updated = record_route_system(state, system_id, at)
        subject = graph.systems[system_id].name
    write_execution_state(arguments.output, updated)
    print(
        f"state: recorded {arguments.action} at {subject}; "
        f"active={len(updated.active_shipments)} -> {arguments.output}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eve-courier", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sde = subparsers.add_parser("sde-info", help="show bundled CCP route-data build")
    sde.set_defaults(handler="sde-info")

    scan = subparsers.add_parser("scan", help="scan public courier contracts via ESI")
    scan.add_argument("--region", action="append", required=True, help="region ID or exact name")
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--cache", type=Path, default=default_cache_path())
    scan.add_argument(
        "--threat-intel",
        action="store_true",
        help="capture gate-focused zKill intel",
    )
    scan.add_argument("--zkill-cache", type=Path, default=default_zkill_cache_path())
    scan.add_argument(
        "--threat-region",
        action="append",
        default=[],
        help=(
            "optional zKill-only transit region ID/name; repeat as needed. If omitted, threat "
            "intel uses the contract regions. The web UI computes this scope automatically"
        ),
    )
    scan.add_argument(
        "--threat-window-hours",
        type=_positive_int,
        default=DEFAULT_THREAT_WINDOW_SECONDS // 3_600,
    )
    scan.add_argument("--gate-radius-km", type=_positive_int, default=250)
    scan.add_argument(
        "--scan-workers",
        type=_positive_int,
        choices=range(1, MAX_CONTRACT_SCAN_WORKERS + 1),
        default=DEFAULT_CONTRACT_SCAN_WORKERS,
        metavar=f"1-{MAX_CONTRACT_SCAN_WORKERS}",
        help="concurrent ESI contract regions; pagination within each region remains sequential",
    )
    scan.set_defaults(handler="scan")

    rank = subparsers.add_parser("rank", help="iteration-2 solo courier profitability ranking")
    rank.add_argument("--snapshot", type=Path, required=True)
    rank.add_argument("--limit", type=int, default=20)
    _add_planning_arguments(rank)
    rank.set_defaults(handler="rank")

    solve = subparsers.add_parser("solve", help="exact interleaved pickup/dropoff optimization")
    solve.add_argument("--snapshot", type=Path, required=True)
    solve.add_argument("--output", type=Path, required=True)
    solve.add_argument("--state-output", type=Path)
    _add_planning_arguments(solve)
    _add_solver_arguments(solve)
    solve.set_defaults(handler="solve")

    replan = subparsers.add_parser("replan", help="solve again with mandatory live commitments")
    replan.add_argument("--snapshot", type=Path, required=True)
    replan.add_argument("--state", type=Path, required=True)
    replan.add_argument("--output", type=Path, required=True)
    replan.add_argument("--state-output", type=Path)
    replan.add_argument("--max-candidates", type=int)
    _add_solver_arguments(replan)
    replan.set_defaults(handler="replan")

    advance = subparsers.add_parser(
        "advance",
        help="record a real pickup, delivery, or required route-system visit",
    )
    advance.add_argument("--state", type=Path, required=True)
    advance.add_argument("--snapshot", type=Path)
    advance.add_argument(
        "--action",
        choices=["pickup", "delivery", "route-system"],
        required=True,
    )
    advance.add_argument("--contract-id", type=int)
    advance.add_argument("--system", help="system ID or exact name for route-system")
    advance.add_argument("--at", default="now", help="ISO timestamp or 'now'")
    advance.add_argument("--output", type=Path, required=True)
    advance.set_defaults(handler="advance")

    web = subparsers.add_parser("web", help="launch the loopback-only localhost control deck")
    web.add_argument("--port", type=int, default=8765, help="localhost port (default: 8765)")
    web.add_argument(
        "--workspace",
        type=Path,
        default=default_web_workspace(),
        help="directory for the cached snapshot, plan and live execution state",
    )
    web.add_argument(
        "--no-browser",
        action="store_true",
        help="start the server without opening the default browser",
    )
    web.set_defaults(handler="web")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        graph = load_bundled_graph()
        if arguments.handler == "sde-info":
            print(
                f"SDE build {graph.metadata.build_number}, released {graph.metadata.release_date}; "
                f"{len(graph.systems)} systems, {len(graph.station_systems)} NPC stations"
            )
            return 0
        if arguments.handler == "scan":
            return _run_scan(arguments, graph)
        if arguments.handler == "rank":
            return _run_rank(arguments, graph)
        if arguments.handler == "solve":
            return _run_solve(arguments, graph)
        if arguments.handler == "replan":
            return _run_replan(arguments, graph)
        if arguments.handler == "advance":
            return _run_advance(arguments, graph)
        if arguments.handler == "web":
            return run_local_web_ui(
                graph,
                port=arguments.port,
                workspace=arguments.workspace,
                open_browser=not arguments.no_browser,
            )
        raise AssertionError(f"unknown handler {arguments.handler}")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
