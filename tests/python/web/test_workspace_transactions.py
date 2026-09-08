from __future__ import annotations

import json
import multiprocessing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eve_courier_optimizer.application.file_lock import WorkspaceConflict, directory_write_lock
from eve_courier_optimizer.application.trip_file import trip_to_dict, write_trip
from eve_courier_optimizer.cli import main
from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.eve.snapshot_file import write_snapshot
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.background_jobs import BackgroundJobs
from eve_courier_optimizer.web.operations import ScanObservation
from eve_courier_optimizer.web.workspace import PlanningWorkspace
from tests.support.clock import MutableClock
from tests.support.web import CourierTransport, planning_payload, proposal_input
from tests.support.workspace_process import stale_reset


@pytest.fixture
def planned(now: datetime, tiny_graph: UniverseGraph, tmp_path: Path) -> PlanningWorkspace:
    clock = MutableClock(now)
    app = PlanningWorkspace(
        tiny_graph, EsiClient(transport=CourierTransport(clock.value)), tmp_path, clock=clock
    )
    app.scan({"regions": [10]})
    assert app.snapshot is not None
    app.solve(planning_payload())
    return app


def test_scans_follow_the_workspace_clock(
    now: datetime, tiny_graph: UniverseGraph, tmp_path: Path
) -> None:
    clock = MutableClock(now)
    app = PlanningWorkspace(
        tiny_graph, EsiClient(transport=CourierTransport(now)), tmp_path, clock=clock
    )
    for elapsed in (timedelta(), timedelta(minutes=5)):
        clock.value = now + elapsed
        app.scan({"regions": [10]})
        assert app.snapshot is not None
        assert app.snapshot.fetched_at == now + elapsed
        assert app.snapshot.system_kills_fetched_at == now + elapsed


def test_acceptance_is_bound_to_exact_proposal_even_when_the_problem_is_identical(
    planned: PlanningWorkspace,
) -> None:
    reviewed = proposal_input(planned, {"confirm_locked_acceptance": True})
    first_plan = planned.plan
    planned.solve(planning_payload())
    assert planned.plan is not None and first_plan is not None
    assert planned.plan.problem == first_plan.problem
    assert planned.proposal_id != reviewed["proposal_id"]
    before = planned.store.path.read_bytes()
    with pytest.raises(WorkspaceConflict):
        planned.start_execution(reviewed)
    assert planned.status()["execution"] is None
    assert planned.store.path.read_bytes() == before
    planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
    assert planned.trip is not None and len(planned.trip.active_shipments) == 1


def test_recording_progress_expires_a_reviewed_proposal(planned: PlanningWorkspace) -> None:
    planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
    planned.replan({"refresh": False})
    reviewed = proposal_input(planned, {"confirm_locked_acceptance": True})
    planned.record_action({"action": "pickup", "contract_id": 9001})
    with pytest.raises(WorkspaceConflict):
        planned.start_execution(reviewed)
    assert planned.trip is not None and planned.trip.active_shipments[0].picked


def test_a_separate_stale_process_cannot_erase_new_commitments(planned: PlanningWorkspace) -> None:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=stale_reset, args=(planned.graph, planned.workspace, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv() == planned.revision
        planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
        parent.send("reset")
        assert parent.poll(10)
        assert parent.recv() == "conflict"
        process.join(10)
        assert process.exitcode == 0
        restored = PlanningWorkspace(
            planned.graph, EsiClient(), planned.workspace, clock=planned.clock
        )
        assert restored.trip == planned.trip
        # Exiting the competing process releases its lock; ordinary progress can continue.
        restored.record_action({"action": "pickup", "contract_id": 9001})
        assert restored.trip is not None and restored.trip.active_shipments[0].picked
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        process.close()
        parent.close()


@pytest.mark.parametrize("publication", ["scan", "parent"])
def test_failed_publication_preserves_the_complete_previous_state(
    planned: PlanningWorkspace, monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    before = planned.store.path.read_bytes()
    identity = proposal_input(planned)
    snapshot = planned.snapshot
    assert snapshot is not None
    original = Path.replace

    def fail(path: Path, target: str | Path) -> Path:
        if target == planned.store.path:
            raise OSError("disk full at commit")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        if publication == "scan":
            planned.scan({"regions": [10]})
        else:
            planned.publish(ScanObservation(snapshot), expected_revision=planned.revision)
    assert planned.store.path.read_bytes() == before
    assert planned.snapshot is snapshot
    assert proposal_input(planned) == identity
    assert not tuple(planned.workspace.glob(".workspace.json.*"))


def test_background_parent_write_failure_preserves_the_previous_proposal(
    planned: PlanningWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import eve_courier_optimizer.web.workspace_store as storage

    before = planned.store.path.read_bytes()
    identity = proposal_input(planned)

    def fail(path: Path, payload: object) -> None:
        raise OSError("disk full at parent publication")

    monkeypatch.setattr(storage, "write_json", fail)
    jobs = BackgroundJobs(planned)
    try:
        jobs.start("scan", {"regions": [10]})
        deadline = time.monotonic() + 15
        while jobs.running and time.monotonic() < deadline:
            time.sleep(0.02)
        job = jobs.status()
        assert job is not None and job["status"] == "failed", job
        assert "parent publication" in job["error"]
    finally:
        jobs.close()
    assert planned.store.path.read_bytes() == before
    assert proposal_input(planned) == identity


@pytest.mark.parametrize("corruption", ["json", "model"])
def test_corrupt_legacy_plan_does_not_hide_an_authoritative_trip(
    planned: PlanningWorkspace, tmp_path: Path, corruption: str
) -> None:
    plan = planned.artifact("plan.json")
    planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
    assert planned.trip is not None and planned.snapshot is not None
    legacy = tmp_path / "legacy"
    write_trip(legacy / "execution.json", planned.trip)
    write_snapshot(legacy / "snapshot.json", planned.snapshot)
    assert isinstance(plan, dict)
    if corruption == "json":
        raw = "{broken"
    else:
        plan["model"]["start_system_id"] = []
        raw = json.dumps(plan)
    (legacy / "plan.json").write_text(raw)
    restored = PlanningWorkspace(planned.graph, EsiClient(), legacy)
    assert restored.trip == planned.trip
    assert restored.status()["warnings"]
    assert restored.plan_payload is None
    assert (legacy / "plan.json").read_text() == raw


def test_sde_upgrade_can_refresh_without_discarding_the_active_trip(
    planned: PlanningWorkspace,
) -> None:
    planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
    planned.record_action({"action": "pickup", "contract_id": 9001})
    previous = planned.trip
    assert previous is not None
    graph = planned.graph
    upgraded = UniverseGraph(
        systems=graph.systems,
        adjacency=graph.adjacency,
        station_systems=graph.station_systems,
        regions=graph.regions,
        metadata=replace(graph.metadata, build_number=2),
    )
    app = PlanningWorkspace(upgraded, planned.esi, planned.workspace, clock=planned.clock)
    assert app.status()["snapshot"] is None
    assert app.observation is not None
    assert app.trip == previous
    with pytest.raises(ValueError, match="refresh enabled"):
        app.replan({"refresh": False})
    app.replan({"refresh": True})
    assert app.snapshot is not None and app.snapshot.sde_build_number == 2
    assert app.trip == previous
    app.start_execution(proposal_input(app))
    assert app.trip is not None
    assert app.trip.active_shipments == previous.active_shipments
    assert app.trip.session_deadline == previous.session_deadline


def test_standalone_cli_cannot_overwrite_managed_workspace_artifacts(
    planned: PlanningWorkspace,
) -> None:
    before = planned.store.path.read_bytes()
    assert (
        main(["scan", "--region", "10", "--output", str(planned.workspace / "snapshot.json")]) != 0
    )
    assert planned.store.path.read_bytes() == before


def test_cli_writes_use_the_same_process_lock(planned: PlanningWorkspace, tmp_path: Path) -> None:
    planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
    assert planned.trip is not None
    directory = tmp_path / "standalone"
    path = directory / "state.json"
    write_trip(path, planned.trip)
    with directory_write_lock(directory):
        assert main(["extend", "--state", str(path), "--output", str(path), "--minutes", "30"]) == 1
    assert json.loads(path.read_text()) == trip_to_dict(planned.trip)


def test_rolling_policy_only_changes_when_the_reviewed_replan_is_applied(
    planned: PlanningWorkspace,
) -> None:
    from tests.support.rolling_policy import prepare_rolling_policy_refresh

    prepare_rolling_policy_refresh(planned, planned.clock())
    old_trip = planned.trip
    assert old_trip is not None and 2 in old_trip.security.threat_avoided_system_ids
    result = planned.replan({"refresh": True})
    assert result["plan"]["summary"]["selected_contract_ids"] == [9001]
    assert planned.trip == old_trip
    with pytest.raises(ValueError):
        planned.record_action({"action": "pickup", "contract_id": 9001})
    planned.start_execution(proposal_input(planned))
    assert planned.trip is not None
    planned.record_action({"action": "pickup", "contract_id": 9001})
    assert planned.trip is not None
    assert not planned.trip.security.threat_avoided_system_ids
    assert planned.trip.active_shipments[0].picked


def test_missing_accepted_endpoint_after_sde_upgrade_preserves_the_trip(
    planned: PlanningWorkspace,
) -> None:
    planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
    before = planned.store.path.read_bytes()
    graph = planned.graph
    upgraded = UniverseGraph(
        systems={key: value for key, value in graph.systems.items() if key != 2},
        adjacency={
            key: tuple(n for n in neighbors if n != 2)
            for key, neighbors in graph.adjacency.items()
            if key != 2
        },
        station_systems={key: value for key, value in graph.station_systems.items() if value != 2},
        regions=graph.regions,
        metadata=replace(graph.metadata, build_number=2),
    )
    app = PlanningWorkspace(upgraded, planned.esi, planned.workspace, clock=planned.clock)
    with pytest.raises(ValueError, match="unknown|required|missing|system"):
        app.replan({"refresh": True})
    assert app.trip == planned.trip
    assert planned.store.path.read_bytes() == before


@pytest.mark.parametrize("corruption", ["malformed", "missing"])
def test_corrupt_authoritative_execution_is_never_replaced_with_an_empty_trip(
    planned: PlanningWorkspace,
    corruption: str,
) -> None:
    planned.start_execution(proposal_input(planned, {"confirm_locked_acceptance": True}))
    data = json.loads(planned.store.path.read_text())
    if corruption == "malformed":
        data["execution"]["active_shipments"] = "corrupt"
    else:
        del data["execution"]
    planned.store.path.write_text(json.dumps(data))
    before = planned.store.path.read_bytes()
    with pytest.raises(ValueError):
        PlanningWorkspace(planned.graph, EsiClient(), planned.workspace)
    assert planned.store.path.read_bytes() == before
