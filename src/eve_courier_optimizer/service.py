"""Application service boundary shared by CLI today and a localhost UI later."""

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
        prepared = self.prepare(
            snapshot,
            constraints,
            active_shipments=active_shipments,
            excluded_contract_ids=excluded_contract_ids,
            max_candidates=max_candidates,
        )
        return prepared, solve_exact(prepared, self.graph, config=solver_config)

    def replan(
        self,
        snapshot: ContractSnapshot,
        state: ExecutionState,
        *,
        max_candidates: int | None = None,
        solver_config: SolverConfig | None = None,
    ) -> tuple[PreparedProblem, SolveResult]:
        constraints = constraints_for_replan(state, snapshot)
        return self.solve(
            snapshot,
            constraints,
            active_shipments=state.active_shipments,
            excluded_contract_ids=frozenset(state.completed_contract_ids),
            max_candidates=max_candidates,
            solver_config=solver_config,
        )
