"""Application workflow shared by the command-line and local web interfaces."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from eve_courier_optimizer import optimization
from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.domain import (
    ActiveShipment,
    ContractSnapshot,
    PlanningConstraints,
    SolveResult,
    planned_visits,
)
from eve_courier_optimizer.eve import contract_scan
from eve_courier_optimizer.eve.esi import EsiClient, utc_now
from eve_courier_optimizer.eve.zkill import (
    DEFAULT_GATE_RADIUS_M,
    DEFAULT_THREAT_WINDOW_SECONDS,
    ZkillClient,
)
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.security import reachable_threat_regions
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification import route_replay


@dataclass(frozen=True, slots=True)
class RoutePlan:
    problem: RouteProblem
    result: SolveResult


class CourierPlanner:
    """Coordinate scanning, preprocessing, solving, and in-progress replanning."""

    def __init__(
        self,
        graph: UniverseGraph,
        esi: EsiClient,
        zkill: ZkillClient | None = None,
        *,
        progress: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.graph = graph
        self.esi = esi
        self.zkill = zkill
        self.progress = progress
        self.clock = clock

    def scan(
        self,
        region_ids: Iterable[int],
        *,
        include_threat_intel: bool = False,
        threat_window_seconds: int = DEFAULT_THREAT_WINDOW_SECONDS,
        threat_gate_radius_m: int = DEFAULT_GATE_RADIUS_M,
        threat_region_ids: Iterable[int] | None = None,
        contract_workers: int = contract_scan.DEFAULT_CONTRACT_SCAN_WORKERS,
    ) -> ContractSnapshot:
        """Fetch an immutable public-contract snapshot and optional gate-threat data."""

        # Keep the legacy ESI aggregate available for old policies. Gate-focused zKill collection
        # remains opt-in and is recorded in the same immutable snapshot boundary.
        return contract_scan.scan_public_couriers(
            self.esi,
            self.graph,
            region_ids,
            include_system_kills=True,
            zkill=self.zkill,
            include_threat_intel=include_threat_intel,
            threat_window_seconds=threat_window_seconds,
            threat_gate_radius_m=threat_gate_radius_m,
            threat_region_ids=threat_region_ids,
            contract_workers=contract_workers,
            progress=self.progress,
            clock=self.clock,
        )

    def solve(
        self,
        snapshot: ContractSnapshot,
        constraints: PlanningConstraints,
        *,
        active_shipments: tuple[ActiveShipment, ...] = (),
        excluded_contract_ids: frozenset[int] = frozenset(),
        max_candidates: int | None = None,
        solver_config: optimization.SolverConfig | None = None,
    ) -> RoutePlan:
        """Prepare and solve a fresh route, returning both the auditable input and result."""

        if self.progress:
            self.progress("Preparing contracts and permitted gate routes")
        problem = RouteProblem.from_snapshot(
            snapshot,
            self.graph,
            constraints,
            active_shipments=active_shipments,
            excluded_contract_ids=excluded_contract_ids,
            max_candidates=max_candidates,
        )
        result = optimization.RouteOptimizer(
            problem,
            self.graph,
            config=solver_config,
            progress=self.progress,
        ).solve()
        return RoutePlan(problem, result)

    def replan(
        self,
        snapshot: ContractSnapshot,
        state: CourierTrip,
        *,
        max_candidates: int | None = None,
        solver_config: optimization.SolverConfig | None = None,
        at: datetime | None = None,
    ) -> RoutePlan:
        """Solve again from live execution state while preserving accepted commitments."""

        self._require_trip_systems(state)
        replanning_constraints = state.replanning_constraints(snapshot, at=at)
        return self.solve(
            snapshot,
            replanning_constraints,
            active_shipments=state.active_shipments,
            excluded_contract_ids=frozenset(state.completed_contract_ids),
            max_candidates=max_candidates,
            solver_config=solver_config,
        )

    def refresh_for_trip(
        self, snapshot: ContractSnapshot, trip: CourierTrip, *, at: datetime
    ) -> ContractSnapshot:
        self._require_trip_systems(trip)
        threat_enabled = bool(trip.security.threat_categories)
        remaining_seconds = max(
            0, int((trip.session_deadline - max(at, trip.current_time)).total_seconds())
        )
        threat_regions = (
            reachable_threat_regions(
                self.graph,
                start_system_id=trip.current_system_id,
                security=trip.security,
                horizon_seconds=remaining_seconds,
                seconds_per_jump=trip.travel.seconds_per_jump,
            )
            if threat_enabled
            else None
        )
        return self.scan(
            snapshot.region_ids,
            include_threat_intel=threat_enabled,
            threat_window_seconds=trip.security.threat_window_seconds
            or DEFAULT_THREAT_WINDOW_SECONDS,
            threat_gate_radius_m=(
                trip.security.threat_gate_radius_m
                if trip.security.threat_gate_radius_m is not None
                else DEFAULT_GATE_RADIUS_M
            ),
            threat_region_ids=threat_regions,
        )

    def _require_trip_systems(self, trip: CourierTrip) -> None:
        required = {trip.current_system_id, *trip.remaining_required_system_ids}
        if trip.terminal_system_id is not None:
            required.add(trip.terminal_system_id)
        for shipment in trip.active_shipments:
            required.add(shipment.contract.destination_system_id)
            if not shipment.picked:
                required.add(shipment.contract.origin_system_id)
        missing = required.difference(self.graph.systems)
        if missing:
            names = ", ".join(str(system) for system in sorted(missing))
            raise ValueError(f"the current SDE is missing required trip systems: {names}")

    def arm(
        self,
        plan: RoutePlan,
        *,
        at: datetime,
        previous: CourierTrip | None = None,
    ) -> CourierTrip:
        """Recheck the proposed itinerary at departure before accepting it as live state."""

        problem, result = plan.problem, plan.result
        if not result.certificate.feasibility_verified:
            raise ValueError("the plan has no independently verified feasible route")
        constraints = problem.constraints
        if at.tzinfo is None or at < constraints.snapshot_time:
            raise ValueError("departure cannot predate the plan")
        horizon = constraints.horizon_seconds
        if previous is not None:
            if at > previous.session_deadline:
                raise ValueError("planning horizon has ended; extend it and replan before arming")
            horizon = int((previous.session_deadline - at).total_seconds())
        selected = set(result.selected_contract_ids)
        if any(c.contract_id in selected and c.date_expired <= at for c in problem.contracts):
            raise ValueError("a selected listing has expired; refresh and solve before arming")
        constraints = replace(constraints, snapshot_time=at, horizon_seconds=horizon)
        simulation = route_replay.simulate_and_verify(
            replace(problem, constraints=constraints),
            self.graph,
            planned_visits(result.travel_legs),
            result.selected_contract_ids,
        )
        if not simulation.report.valid:
            raise ValueError(
                "departure revalidation failed: " + "; ".join(simulation.report.violations)
            )
        state = CourierTrip.from_plan(
            constraints,
            problem.contracts,
            problem.active_shipments,
            result,
            completed_contract_ids=previous.completed_contract_ids if previous else (),
        )
        return replace(state, session_deadline=previous.session_deadline) if previous else state
