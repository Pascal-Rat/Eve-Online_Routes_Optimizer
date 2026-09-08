"""Compute planning results without publishing files or mutating a workspace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.planner import CourierPlanner, RoutePlan
from eve_courier_optimizer.domain import ContractSnapshot
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.web import requests


class Operation(StrEnum):
    SCAN = "scan"
    RANK = "rank"
    SOLVE = "solve"
    REPLAN = "replan"


@dataclass(frozen=True, slots=True)
class ScanObservation:
    snapshot: ContractSnapshot


@dataclass(frozen=True, slots=True)
class Ranking:
    problem: RouteProblem


@dataclass(frozen=True, slots=True)
class ProposedPlan:
    snapshot: ContractSnapshot
    plan: RoutePlan


OperationResult = ScanObservation | Ranking | ProposedPlan


def compute_operation(
    operation: Operation,
    planner: CourierPlanner,
    body: dict[str, object],
    *,
    observation: ContractSnapshot | None,
    trip: CourierTrip | None,
    at: datetime,
) -> OperationResult:
    graph = planner.graph
    if trip is not None and operation is not Operation.REPLAN:
        raise ValueError("an execution session already exists; use Replan")
    if operation is Operation.SCAN:
        scan = requests.ScanRequest.from_json(body, graph)
        return ScanObservation(
            planner.scan(
                scan.region_ids,
                include_threat_intel=scan.include_threat_intel,
                threat_window_seconds=scan.threat_window_seconds,
                threat_gate_radius_m=scan.threat_gate_radius_m,
                threat_region_ids=scan.threat_region_ids,
            )
        )
    if observation is None:
        raise ValueError("scan at least one region before ranking or solving")
    snapshot = observation
    if operation is Operation.REPLAN:
        if trip is None:
            raise ValueError("start an execution session before replanning")
        replan = requests.ReplanRequest.from_json(body)
        if replan.refresh_snapshot:
            snapshot = planner.refresh_for_trip(snapshot, trip, at=at)
        if snapshot.sde_build_number != graph.metadata.build_number:
            raise ValueError("the saved observation uses an older SDE; replan with refresh enabled")
        return ProposedPlan(
            snapshot,
            planner.replan(
                snapshot,
                trip,
                max_candidates=replan.max_candidates,
                solver_config=replan.solver_config,
                at=at,
            ),
        )
    if snapshot.sde_build_number != graph.metadata.build_number:
        raise ValueError("the saved observation uses an older SDE; scan again")
    request = requests.PlanRequest.from_json(body, snapshot, graph, at)
    if operation is Operation.RANK:
        return Ranking(
            RouteProblem.from_snapshot(
                snapshot, graph, request.constraints, max_candidates=request.max_candidates
            )
        )
    return ProposedPlan(
        snapshot,
        planner.solve(
            snapshot,
            request.constraints,
            max_candidates=request.max_candidates,
            solver_config=requests.read_solver_config(body),
        ),
    )
