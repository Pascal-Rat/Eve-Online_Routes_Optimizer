"""Application workflow shared by the command-line and local web interfaces."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from .domain import (
    ActiveShipment,
    PlanningConstraints,
    SolveResult,
    planned_visits,
)
from .esi import EsiClient
from .execution import ExecutionState, constraints_for_replan, initial_execution_state
from .planning import PreparedProblem, prepare_problem
from .scanner import DEFAULT_CONTRACT_SCAN_WORKERS, scan_public_couriers
from .sde import UniverseGraph
from .search_config import SolverConfig
from .snapshot import ContractSnapshot
from .solver import solve_exact
from .threat_intel import DEFAULT_GATE_RADIUS_M, DEFAULT_THREAT_WINDOW_SECONDS, ZkillClient
from .verification import simulate_and_verify


@dataclass(frozen=True, slots=True)
class RoutePlan:
    prepared: PreparedProblem
    result: SolveResult


class PlannerService:
    """Coordinate scanning, preprocessing, solving, and in-progress replanning."""

    def __init__(
        self,
        graph: UniverseGraph,
        esi: EsiClient,
        zkill: ZkillClient | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.graph = graph
        self.esi = esi
        self.zkill = zkill
        self.progress = progress

    def scan(
        self,
        region_ids: Iterable[int],
        *,
        include_threat_intel: bool = False,
        threat_window_seconds: int = DEFAULT_THREAT_WINDOW_SECONDS,
        threat_gate_radius_m: int = DEFAULT_GATE_RADIUS_M,
        threat_region_ids: Iterable[int] | None = None,
        contract_workers: int = DEFAULT_CONTRACT_SCAN_WORKERS,
    ) -> ContractSnapshot:
        """Fetch an immutable public-contract snapshot and optional gate-threat data."""

        # Keep the legacy ESI aggregate available for old policies. Gate-focused zKill collection
        # remains opt-in and is recorded in the same immutable snapshot boundary.
        return scan_public_couriers(
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
        )

    def prepare(
        self,
        snapshot: ContractSnapshot,
        constraints: PlanningConstraints,
        *,
        active_shipments: tuple[ActiveShipment, ...] = (),
        excluded_contract_ids: frozenset[int] = frozenset(),
        max_candidates: int | None = None,
    ) -> PreparedProblem:
        """Validate and safely reduce a snapshot into the exact solver input."""

        if self.progress:
            self.progress("Preparing contracts and permitted gate routes")
        return prepare_problem(
            snapshot,
            self.graph,
            constraints,
            active_shipments=active_shipments,
            excluded_contract_ids=excluded_contract_ids,
            max_candidates=max_candidates,
        )

    def solve(
        self,
        snapshot: ContractSnapshot,
        constraints: PlanningConstraints,
        *,
        active_shipments: tuple[ActiveShipment, ...] = (),
        excluded_contract_ids: frozenset[int] = frozenset(),
        max_candidates: int | None = None,
        solver_config: SolverConfig | None = None,
    ) -> RoutePlan:
        """Prepare and solve a fresh route, returning both the auditable input and result."""

        prepared_problem = self.prepare(
            snapshot,
            constraints,
            active_shipments=active_shipments,
            excluded_contract_ids=excluded_contract_ids,
            max_candidates=max_candidates,
        )
        result = solve_exact(
            prepared_problem,
            self.graph,
            config=solver_config,
            progress=self.progress,
        )
        return RoutePlan(prepared_problem, result)

    def replan(
        self,
        snapshot: ContractSnapshot,
        state: ExecutionState,
        *,
        max_candidates: int | None = None,
        solver_config: SolverConfig | None = None,
        at: datetime | None = None,
    ) -> RoutePlan:
        """Solve again from live execution state while preserving accepted commitments."""

        replanning_constraints = constraints_for_replan(state, snapshot, at=at)
        return self.solve(
            snapshot,
            replanning_constraints,
            active_shipments=state.active_shipments,
            excluded_contract_ids=frozenset(state.completed_contract_ids),
            max_candidates=max_candidates,
            solver_config=solver_config,
        )

    def arm(
        self,
        plan: RoutePlan,
        *,
        at: datetime,
        previous: ExecutionState | None = None,
    ) -> ExecutionState:
        """Recheck the proposed itinerary at departure before accepting it as live state."""

        prepared, result = plan.prepared, plan.result
        if not result.certificate.feasibility_verified:
            raise ValueError("the plan has no independently verified feasible route")
        constraints = prepared.problem.constraints
        if at.tzinfo is None or at < constraints.snapshot_time:
            raise ValueError("departure cannot predate the plan")
        horizon = constraints.horizon_seconds
        if previous is not None:
            if at > previous.session_deadline:
                raise ValueError("planning horizon has ended; extend it and replan before arming")
            horizon = int((previous.session_deadline - at).total_seconds())
        selected = set(result.selected_contract_ids)
        if any(
            c.contract_id in selected and c.date_expired <= at for c in prepared.problem.contracts
        ):
            raise ValueError("a selected listing has expired; refresh and solve before arming")
        constraints = replace(constraints, snapshot_time=at, horizon_seconds=horizon)
        replay = simulate_and_verify(
            replace(prepared.problem, constraints=constraints),
            self.graph,
            planned_visits(result.travel_legs),
            result.selected_contract_ids,
        )
        if not replay.report.valid:
            raise ValueError(
                "departure revalidation failed: " + "; ".join(replay.report.violations)
            )
        state = initial_execution_state(
            constraints,
            prepared.problem.contracts,
            prepared.problem.active_shipments,
            result,
            completed_contract_ids=previous.completed_contract_ids if previous else (),
        )
        return replace(state, session_deadline=previous.session_deadline) if previous else state
