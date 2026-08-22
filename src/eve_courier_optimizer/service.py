"""Application workflow shared by the command-line and local web interfaces."""

from __future__ import annotations

from collections.abc import Iterable

from .domain import ActiveShipment, PlanningConstraints, SolveResult
from .esi import EsiClient
from .execution import ExecutionState, constraints_for_replan
from .planning import PreparedProblem, prepare_problem
from .scanner import DEFAULT_CONTRACT_SCAN_WORKERS, scan_public_couriers
from .sde import UniverseGraph
from .snapshot import ContractSnapshot
from .solver import SolverConfig, solve_exact
from .threat_intel import DEFAULT_GATE_RADIUS_M, DEFAULT_THREAT_WINDOW_SECONDS, ZkillClient


class PlannerService:
    """Coordinate scanning, preprocessing, solving, and in-progress replanning."""

    def __init__(
        self,
        graph: UniverseGraph,
        esi: EsiClient,
        zkill: ZkillClient | None = None,
    ) -> None:
        self.graph = graph
        self.esi = esi
        self.zkill = zkill

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
    ) -> tuple[PreparedProblem, SolveResult]:
        """Prepare and solve a fresh route, returning both the auditable input and result."""

        prepared_problem = self.prepare(
            snapshot,
            constraints,
            active_shipments=active_shipments,
            excluded_contract_ids=excluded_contract_ids,
            max_candidates=max_candidates,
        )
        return prepared_problem, solve_exact(
            prepared_problem,
            self.graph,
            config=solver_config,
        )

    def replan(
        self,
        snapshot: ContractSnapshot,
        state: ExecutionState,
        *,
        max_candidates: int | None = None,
        solver_config: SolverConfig | None = None,
    ) -> tuple[PreparedProblem, SolveResult]:
        """Solve again from live execution state while preserving accepted commitments."""

        replanning_constraints = constraints_for_replan(state, snapshot)
        return self.solve(
            snapshot,
            replanning_constraints,
            active_shipments=state.active_shipments,
            excluded_contract_ids=frozenset(state.completed_contract_ids),
            max_candidates=max_candidates,
            solver_config=solver_config,
        )
