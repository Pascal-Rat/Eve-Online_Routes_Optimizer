from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import eve_courier_optimizer.cli as cli_module
from eve_courier_optimizer.cli import main
from eve_courier_optimizer.domain import PlanningConstraints, SecurityPolicy, TravelTimeModel
from eve_courier_optimizer.esi import EsiClient, HttpResponse
from eve_courier_optimizer.reporting import solve_result_to_dict, write_solve_result
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.service import PlannerService
from eve_courier_optimizer.snapshot import ContractSnapshot, write_snapshot
from eve_courier_optimizer.solver import SolverConfig

from .conftest import make_contract, make_snapshot


class EmptyTransport:
    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        del url, headers, timeout_seconds
        return HttpResponse(200, {"x-pages": "1"}, b"[]")


def test_service_and_reporting(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    graph = tiny_graph
    snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    constraints = PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=20,
        collateral_budget_units=200,
        horizon_seconds=1_000,
        snapshot_time=now,
        travel=TravelTimeModel(10, 1),
        security=SecurityPolicy(0.45),
    )
    service = PlannerService(graph, EsiClient(transport=EmptyTransport()))
    prepared, result = service.solve(
        snapshot,
        constraints,
        solver_config=SolverConfig(max_time_seconds=10),
    )
    payload = solve_result_to_dict(result, prepared.problem)
    assert payload["schema_version"] == 3
    assert payload["certificate"]["status"] == "proven_optimal"
    assert "bound_strengthening" in payload["certificate"]
    assert payload["summary"]["total_reward_isk"] == "5"
    assert payload["scope"]["sde_build_number"] == 1
    assert payload["scope"]["snapshot_compatibility_date"] == "2026-08-05"
    assert payload["model"]["return_to_start"] is True
    assert payload["model"]["terminal_system_id"] == 1
    assert payload["travel_legs"][-1]["kind"] == "finish"
    assert payload["travel_legs"][-1]["to_system_id"] == 1
    output = tmp_path / "plan.json"
    write_solve_result(output, result, prepared.problem)
    assert json.loads(output.read_text())["route"][0]["action"] == "pickup"
    scanned = service.scan([10])
    assert not scanned.contracts


def test_cli_sde_info_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["sde-info"])
    assert exit_code == 0
    assert "SDE build" in capsys.readouterr().out


def test_cli_solve_advance_and_replan_workflow(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module, "load_bundled_graph", lambda: tiny_graph)
    public = make_contract(now, 1, 101, 102, reward=500)
    snapshot = make_snapshot(now, public)
    snapshot_path = tmp_path / "snapshot.json"
    write_snapshot(snapshot_path, snapshot)

    common = [
        "--snapshot",
        str(snapshot_path),
        "--start",
        "Alpha",
        "--cargo-m3",
        "1",
        "--collateral-isk",
        "2",
        "--hours",
        "0.1",
        "--seconds-per-jump",
        "10",
        "--service-seconds",
        "1",
    ]
    assert main(["rank", *common, "--limit", "5"]) == 0
    assert "contract_id" in capsys.readouterr().out

    plan_path = tmp_path / "plan.json"
    state_path = tmp_path / "accepted-state.json"
    assert (
        main(
            [
                "solve",
                *common,
                "--output",
                str(plan_path),
                "--state-output",
                str(state_path),
                "--time-limit",
                "10",
                "--require-global-optimal",
            ]
        )
        == 0
    )
    assert json.loads(plan_path.read_text())["certificate"]["status"] == "proven_optimal"

    picked_state = tmp_path / "picked-state.json"
    pickup_at = (now + timedelta(minutes=1)).isoformat()
    assert (
        main(
            [
                "advance",
                "--state",
                str(state_path),
                "--snapshot",
                str(snapshot_path),
                "--action",
                "pickup",
                "--contract-id",
                "1",
                "--at",
                pickup_at,
                "--output",
                str(picked_state),
            ]
        )
        == 0
    )

    # Once accepted, the contract disappears from the public scan. The mandatory shipment lives
    # in execution state and must still be delivered by the replanner.
    refreshed = ContractSnapshot(
        fetched_at=now + timedelta(minutes=1),
        compatibility_date=snapshot.compatibility_date,
        sde_build_number=1,
        region_ids=(10,),
        contracts=(),
    )
    refreshed_path = tmp_path / "refreshed.json"
    write_snapshot(refreshed_path, refreshed)
    replan_path = tmp_path / "replan.json"
    replan_state = tmp_path / "replan-state.json"
    assert (
        main(
            [
                "replan",
                "--snapshot",
                str(refreshed_path),
                "--state",
                str(picked_state),
                "--output",
                str(replan_path),
                "--state-output",
                str(replan_state),
                "--time-limit",
                "10",
                "--require-global-optimal",
            ]
        )
        == 0
    )
    assert json.loads(replan_path.read_text())["route"][0]["action"] == "delivery"

    delivered_state = tmp_path / "delivered-state.json"
    assert (
        main(
            [
                "advance",
                "--state",
                str(replan_state),
                "--snapshot",
                str(refreshed_path),
                "--action",
                "delivery",
                "--contract-id",
                "1",
                "--at",
                (now + timedelta(minutes=2)).isoformat(),
                "--output",
                str(delivered_state),
            ]
        )
        == 0
    )
