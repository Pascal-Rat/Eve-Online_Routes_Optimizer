from __future__ import annotations

import json
import sys
import threading
import webbrowser
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import eve_courier_optimizer.cli as cli_module
import eve_courier_optimizer.web.server as webapp_module
from eve_courier_optimizer.application.file_lock import WorkspaceConflict
from eve_courier_optimizer.cli import main
from eve_courier_optimizer.domain import GateEvidence, GateThreatEvent, SolveResult, ThreatCategory
from eve_courier_optimizer.eve.esi import EsiClient, EsiError
from eve_courier_optimizer.eve.http import RequestBudget
from eve_courier_optimizer.eve.zkill import ZkillClient
from eve_courier_optimizer.routing.universe import Region, UniverseGraph
from eve_courier_optimizer.web.operations import ProposedPlan
from eve_courier_optimizer.web.server import asset, create_http_server, run_local_web_ui
from eve_courier_optimizer.web.workspace import PlanningWorkspace, default_workspace_path
from tests.support.assertions import present
from tests.support.web import (
    CourierTransport,
    planning_payload,
    post_json,
    proposal_input,
    seed_snapshot,
)


@pytest.mark.parametrize(
    "failure, status",
    [
        (ValueError("invalid input"), 400),
        (EsiError("feed unavailable"), 502),
        (RuntimeError("solver failure"), 500),
        (OSError("disk unavailable"), 500),
    ],
)
def test_http_distinguishes_invalid_input_external_failure_and_internal_failure(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    status: int,
) -> None:
    app = PlanningWorkspace(tiny_graph, EsiClient(), tmp_path)

    def fail(_body: dict[str, object]) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(app, "solve", fail)
    server = create_http_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as caught:
            post_json(f"http://127.0.0.1:{server.server_port}/api/solve", {})
        assert caught.value.code == status
        assert json.load(caught.value) == {"error": str(failure)}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_application_full_locked_workflow(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    transport = CourierTransport(datetime.now(UTC))
    app = PlanningWorkspace(tiny_graph, EsiClient(transport=transport), tmp_path)

    assert app.region_matches("test")["items"] == [{"id": 10, "name": "Test Region"}]
    assert app.system_matches("alp")["items"][0]["name"] == "Alpha"
    assert app.system_matches("a") == {"items": []}

    scan = app.scan({"regions": ["Test Region"]})
    assert present(scan["snapshot"])["contracts"] == 1
    assert app.artifact("snapshot.json") is not None
    assert transport.calls == 2

    ranked = app.rank(planning_payload())
    assert ranked["scope"]["scope_untruncated"] is True
    assert ranked["items"][0]["contract_id"] == 9001

    plan = app.solve(planning_payload())["plan"]
    assert plan["certificate"]["status"] == "proven_optimal"
    assert plan["certificate"]["scope_untruncated"] is True
    assert plan["certificate"]["feasibility_verified"] is True
    assert plan["route"][0].get("system_name") == "Alpha"
    assert plan["route"][0].get("title") == "Alpha to Beta test load"
    assert [system["name"] for system in plan["route"][1].get("jump_path_systems", [])] == [
        "Alpha",
        "Beta",
    ]
    assert plan["route"][1].get("jump_path_systems", [])[-1]["security_band"] == "high"
    assert app.artifact("plan.json") is not None

    with pytest.raises(ValueError, match="requires confirmation"):
        app.start_execution(proposal_input(app, {"confirm_locked_acceptance": False}))
    started = app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))[
        "execution"
    ]
    assert present(started)["active_count"] == 1
    assert present(started)["active_shipments"][0]["picked"] is False
    assert app.artifact("execution.json") is not None

    restored = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )
    restored_status = restored.status()
    assert present(restored_status["snapshot"])["contracts"] == 1
    assert present(restored_status["execution"])["active_count"] == 1
    assert present(restored_status["execution"])["can_end_safely"] is False
    assert present(restored_status["plan"])["model"].get("start_system_name") == "Alpha"
    assert present(restored_status["plan"])["route"][0].get("title") == "Alpha to Beta test load"
    assert (
        present(restored_status["plan"])["route"][1].get("jump_path_systems", [])[-1]["name"]
        == "Beta"
    )

    # Arming is deliberately one-shot: it cannot rewind a live session to an older solved state.
    with pytest.raises(WorkspaceConflict, match="no longer current"):
        app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))

    picked = app.record_action({"action": "pickup", "contract_id": 9001, "at": "now"})["execution"]
    assert present(picked)["active_shipments"][0]["picked"] is True
    refreshed_plan = app.replan({**planning_payload(), "refresh": True})["plan"]
    assert refreshed_plan["route"][-1]["action"] == "delivery"
    delivered = app.record_action({"action": "delivery", "contract_id": 9001, "at": "now"})[
        "execution"
    ]
    assert present(delivered)["active_count"] == 0
    assert present(delivered)["can_end_safely"] is True
    assert present(delivered)["completed_contract_ids"] == [9001]

    # A stale public observation cannot make an already-delivered contract profitable twice.
    replanned = app.replan({**planning_payload(), "refresh": False})["plan"]
    assert replanned["summary"]["selected_contract_ids"] == []
    assert replanned["scope"]["safe_reductions"] == {"completed_in_session": 1}

    app.reset_execution()
    assert app.artifact("execution.json") is None


def test_web_validation_errors_are_explicit(tiny_graph: UniverseGraph, tmp_path: Path) -> None:
    app = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )
    with pytest.raises(ValueError, match="scan at least"):
        app.rank(planning_payload())
    invalid_regions: list[object] = [[], "Test Region"]
    for regions in invalid_regions:
        with pytest.raises(ValueError, match="at least one"):
            app.scan({"regions": regions})
    for region in ["", "Missing Region", 999]:
        with pytest.raises(ValueError):
            app.scan({"regions": [region]})

    # Numeric SDE IDs are accepted as well as exact names.
    assert present(app.scan({"regions": [10]})["snapshot"])["region_ids"] == [10]

    invalid_updates: list[tuple[str, object, str]] = [
        ("security", "unsafe", "security"),
        ("collateral_mode", "mystery", "collateral mode"),
        ("avoid_systems", {"Alpha": True}, "avoid_systems"),
        ("start", "", "start system"),
        ("start", "Missing", "resolve unique system"),
        ("start", "999", "unknown system"),
        ("hours", "banana", "hours must be a number"),
        ("hours", "0", "positive finite"),
        ("cargo_m3", "nope", "cargo_m3 must be a number"),
        ("cargo_m3", "-1", "non-negative finite"),
        ("collateral_isk", "nope", "collateral must be a number"),
        ("collateral_isk", "-1", "collateral must be a number"),
        ("max_candidates", "nope", "must be an integer"),
        ("max_candidates", "0", "must be positive"),
        ("required_systems", {"Gamma": True}, "required_systems"),
        ("max_simultaneous_contracts", "nope", "must be an integer"),
        ("max_simultaneous_contracts", "-1", "cannot be negative"),
    ]
    for key, value, message in invalid_updates:
        body = {**planning_payload(), key: value}
        with pytest.raises(ValueError, match=message):
            app.rank(body)

    # String-form avoid lists and unrestricted security exercise the alternate policy path.
    alternate = {
        **planning_payload(),
        "start": "1",
        "security": "any",
        "avoid_systems": "Low, Island",
    }
    assert app.rank(alternate)["items"][0]["contract_id"] == 9001

    for update, message in [
        ({"time_limit": "nope"}, "must be numeric"),
        ({"time_limit": "nan"}, "positive finite"),
        ({"workers": "0"}, "num_workers must be positive"),
    ]:
        with pytest.raises(ValueError, match=message):
            app.solve({**planning_payload(), **update})

    app.solve(planning_payload())
    assert app.plan is not None
    saved_plan = app.plan
    result = app.plan.result
    bad_certificate = replace(result.certificate, feasibility_verified=False)
    snapshot = present(app.snapshot)
    app.publish(
        ProposedPlan(
            snapshot,
            replace(
                saved_plan,
                result=SolveResult(
                    result.selected_contract_ids,
                    result.route,
                    result.total_reward_units,
                    result.finish_seconds,
                    bad_certificate,
                ),
            ),
        ),
        expected_revision=app.revision,
    )
    with pytest.raises(ValueError, match="no independently verified"):
        app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
    app.publish(ProposedPlan(snapshot, saved_plan), expected_revision=app.revision)
    app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
    with pytest.raises(ValueError, match="execution session already exists"):
        app.solve(planning_payload())
    with pytest.raises(ValueError, match="contract_id must be an integer"):
        app.record_action({"action": "pickup", "contract_id": "x"})
    with pytest.raises(ValueError, match="action must"):
        app.record_action({"action": "dance", "contract_id": 9001})


def test_web_route_shape_controls_support_zero_cargo_waypoint_and_fixed_finish(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    app = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )
    app.scan({"regions": [10]})
    route_only = {
        **planning_payload(),
        "cargo_m3": "0",
        "required_systems": ["Gamma"],
        "return_to_start": True,
        "max_simultaneous_contracts": "0",
    }
    plan = app.solve(route_only)["plan"]
    assert plan["summary"]["selected_contract_ids"] == []
    assert plan["route"] == []
    assert plan["model"]["return_to_start"] is True
    assert plan["model"]["required_system_ids"] == [3]
    assert plan["model"]["terminal_system_id"] == 1
    assert plan["model"]["max_simultaneous_contracts"] == 0
    assert [leg["kind"] for leg in plan["travel_legs"]] == ["waypoint", "finish"]
    assert plan["travel_legs"][0].get("to_system_name") == "Gamma"
    assert plan["travel_legs"][-1].get("to_system_name") == "Alpha"

    execution = app.start_execution(proposal_input(app, {}))["execution"]
    assert present(execution)["active_count"] == 0
    assert present(execution)["can_end_safely"] is True
    assert present(execution)["remaining_required_system_ids"] == [3]
    reached = app.record_action({"action": "route_system", "system_id": 3, "at": "now"})[
        "execution"
    ]
    assert present(reached)["current_system_name"] == "Gamma"
    assert present(reached)["remaining_required_system_ids"] == []

    app.reset_execution()
    fixed_finish = app.solve(
        {
            **planning_payload(),
            "cargo_m3": "0",
            "return_to_start": False,
            "finish_system": "Gamma",
        }
    )["plan"]
    assert fixed_finish["model"]["finish_system_id"] == 3
    assert fixed_finish["model"]["terminal_system_id"] == 3
    assert fixed_finish["travel_legs"][-1]["jump_path"] == [1, 2, 3]


def test_web_modern_controls_record_security_time_isk_and_gank_policy(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    app = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )
    scanned = app.scan({"region_scope": "all"})["snapshot"]
    assert present(scanned)["region_ids"] == [10]
    assert present(scanned)["system_kill_systems"] == 3
    assert present(scanned)["system_kills_fetched_at"] is not None

    modern = {
        **planning_payload(),
        "collateral_isk": "2B",
        "collateral_unit": "m",  # Explicit suffix wins, preventing accidental double scaling.
        "duration_hours": "0",
        "duration_minutes": "6",
        "security_bands": ["high", "low"],
        "gank_awareness": True,
        "gank_ship_kill_threshold": "10",
    }
    modern.pop("hours")
    modern.pop("security")
    plan = app.solve(modern)["plan"]
    assert plan["model"]["horizon_seconds"] == 360
    assert plan["model"]["collateral_budget_isk"] == "2000000000"
    assert plan["model"]["allowed_security_bands"] == ["high", "low"]
    assert plan["model"]["gank_ship_kill_threshold"] == 10
    assert plan["model"]["gank_avoided_system_ids"] == [4]
    assert plan["model"]["gank_activity_fetched_at"] is not None


def test_web_scan_scopes_zkill_to_proof_safe_route_reachable_regions(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    graph = UniverseGraph(
        systems={
            **tiny_graph.systems,
            4: replace(tiny_graph.systems[4], region_id=11),
        },
        adjacency=tiny_graph.adjacency,
        station_systems=tiny_graph.station_systems,
        regions={**tiny_graph.regions, 11: Region(11, "Low Region")},
        metadata=tiny_graph.metadata,
    )

    class RecordingZkillClient(ZkillClient):
        def __init__(self) -> None:
            super().__init__(request_spacing_seconds=0)
            self.regions: list[int] = []

        def region_losses(
            self,
            region_id: int,
            *,
            past_seconds: int = 7_200,
            page: int = 1,
            budget: RequestBudget | None = None,
        ) -> tuple[dict[str, object], ...]:
            assert past_seconds == 7_200
            self.regions.append(region_id)
            return ()

    zkill = RecordingZkillClient()
    app = PlanningWorkspace(
        graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
        zkill,
    )
    scan_body: dict[str, object] = {
        "regions": [10, 11],
        "include_threat_intel": True,
        "threat_scope_to_plan": True,
        "start": "Alpha",
        "duration_hours": 0,
        "duration_minutes": 6,
        "security_bands": ["high"],
        "seconds_per_jump": 10,
    }
    highsec = app.scan(scan_body)["snapshot"]
    assert zkill.regions == [10]
    assert present(highsec)["threat_coverage_region_ids"] == [10]

    app.scan({**scan_body, "security_bands": ["high", "low"]})
    assert zkill.regions == [10, 10, 11]


def test_web_contract_region_presets_use_sde_security_and_faction_metadata(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    graph = UniverseGraph(
        systems={
            **tiny_graph.systems,
            6: replace(
                tiny_graph.systems[1],
                system_id=6,
                region_id=11,
                name="Null",
                security_status=-0.1,
            ),
            7: replace(
                tiny_graph.systems[1],
                system_id=7,
                region_id=12,
                name="Special",
                security_status=1.0,
            ),
        },
        adjacency={**tiny_graph.adjacency, 6: (), 7: ()},
        station_systems=tiny_graph.station_systems,
        regions={
            10: Region(10, "Test Region", faction_id=500001),
            11: Region(11, "Null Region"),
            12: Region(12, "Special Region"),
        },
        metadata=tiny_graph.metadata,
    )
    app = PlanningWorkspace(
        graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )

    high = app.scan({"region_scope": "security", "security_bands": ["high"]})["snapshot"]
    assert present(high)["region_ids"] == [10, 12]
    low = app.scan({"region_scope": "security", "security_bands": ["low"]})["snapshot"]
    assert present(low)["region_ids"] == [10]
    null = app.scan({"region_scope": "security", "security_bands": ["null"]})["snapshot"]
    assert present(null)["region_ids"] == [11]
    empire = app.scan({"region_scope": "empire", "security_bands": ["high"]})["snapshot"]
    assert present(empire)["region_ids"] == [10]
    assert app.status()["sde"]["empire_regions"] == 1

    with pytest.raises(ValueError, match="contains no regions"):
        app.scan({"region_scope": "empire", "security_bands": ["null"]})
    with pytest.raises(ValueError, match="region_scope"):
        app.scan({"region_scope": "mystery"})


def test_web_gate_threat_categories_create_auditable_hard_avoids(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
    now: datetime,
) -> None:
    app = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(now)),
        tmp_path,
    )
    app.scan({"regions": [10]})
    assert app.snapshot is not None
    event = GateThreatEvent(
        killmail_id=7001,
        occurred_at=now,
        system_id=2,
        region_id=10,
        gate_id=500,
        distance_to_gate_m=0,
        evidence=GateEvidence.ZKILL_LOCATION,
        categories=frozenset({ThreatCategory.SMARTBOMB, ThreatCategory.ANY_GATE_PVP}),
        victim_ship_type_id=3001,
        attacker_weapon_type_ids=(2001,),
        player_attacker_count=1,
    )
    seed_snapshot(
        app,
        replace(
            app.snapshot,
            threat_intel_fetched_at=now,
            threat_window_seconds=86_400,
            threat_gate_radius_m=250_000,
            threat_coverage_region_ids=(10,),
            threat_killmails_seen=20,
            gate_threat_events=(event,),
        ),
    )
    body = {
        **planning_payload(),
        "gank_awareness": True,
        "threat_categories": ["smartbomb"],
        "threat_min_events": 1,
    }
    plan = app.solve(body)["plan"]
    assert plan["model"]["threat_avoided_system_ids"] == [2]
    assert plan["model"]["threat_categories"] == ["smartbomb"]
    assert plan["model"]["threat_window_seconds"] == 86_400
    assert plan["model"]["threat_gate_radius_m"] == 250_000
    assert plan["model"]["threat_coverage_region_ids"] == [10]
    assert plan["scope"]["policy_exclusions"] == {"gate_threat_policy": 1}


def test_web_modern_control_validation_and_missing_activity(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    app = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )
    app.scan({"regions": [10]})
    base = {**planning_payload(), "security_bands": ["high"]}
    base.pop("security")
    invalid_modern: list[tuple[dict[str, object], str]] = [
        ({"security_bands": []}, "at least one"),
        ({"security_bands": "high"}, "must be a list"),
        ({"security_bands": ["wormhole"]}, "only high, low, and null"),
        ({"duration_hours": "0", "duration_minutes": "60"}, "between 0 and 59"),
        ({"duration_hours": "0", "duration_minutes": "0"}, "greater than zero"),
        ({"duration_hours": "1.5", "duration_minutes": "0"}, "must be an integer"),
        ({"collateral_unit": "quadrillion"}, "collateral unit"),
        ({"gank_awareness": True, "gank_ship_kill_threshold": "0"}, "must be positive"),
    ]
    for update, message in invalid_modern:
        with pytest.raises(ValueError, match=message):
            app.rank({**base, **update})

    assert app.snapshot is not None
    seed_snapshot(
        app,
        replace(
            app.snapshot,
            system_kills_fetched_at=None,
            system_kill_activity=(),
        ),
    )
    with pytest.raises(ValueError, match="requires system-kill activity"):
        app.rank({**base, "gank_awareness": True, "gank_ship_kill_threshold": 10})


def test_replan_and_actions_require_execution_state(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    app = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )
    with pytest.raises(ValueError, match="start an execution session"):
        app.record_action({"action": "pickup", "contract_id": 1})
    app.scan({"regions": [10]})
    with pytest.raises(ValueError, match="start an execution session"):
        app.replan(planning_payload())


def test_web_http_boundary_serves_assets_and_rejects_nonlocal_host(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    app = PlanningWorkspace(
        tiny_graph,
        EsiClient(transport=CourierTransport(datetime.now(UTC))),
        tmp_path,
    )
    server = create_http_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = server.server_address[0]
    port = server.server_address[1]
    assert isinstance(host, str)
    assert isinstance(port, int)
    base = f"http://{host}:{port}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:  # noqa: S310 - loopback test server
            html = response.read().decode()
            assert response.status == 200
            assert "Optimize &amp; prove" in html
            assert "Content-Security-Policy" in response.headers
        with urlopen(f"{base}/api/status", timeout=2) as response:  # noqa: S310
            status = json.load(response)
            assert status["sde"]["build_number"] == 1
        with urlopen(f"{base}/api/regions?q=Test", timeout=2) as response:  # noqa: S310
            assert json.load(response)["items"][0]["name"] == "Test Region"
        with urlopen(f"{base}/api/systems?q=Al", timeout=2) as response:  # noqa: S310
            assert json.load(response)["items"][0]["name"] == "Alpha"
        with urlopen(f"{base}/styles.css", timeout=2) as response:  # noqa: S310
            assert response.headers["Cache-Control"] == "no-cache"
            assert b"--cyan" in response.read()

        scan = post_json(
            f"{base}/api/scan",
            {"regions": ["Test Region"]},
            origin=f"http://localhost:{port}",
        )
        assert scan["snapshot"]["contracts"] == 1
        with urlopen(f"{base}/download/snapshot.json", timeout=2) as response:  # noqa: S310
            assert response.headers["Content-Disposition"] == 'attachment; filename="snapshot.json"'
            assert json.load(response)["schema_version"] == 2
        ranked = post_json(f"{base}/api/rank", planning_payload())
        assert ranked["items"][0]["contract_id"] == 9001
        solved = post_json(f"{base}/api/solve", planning_payload())
        assert solved["plan"]["certificate"]["status"] == "proven_optimal"
        with urlopen(f"{base}/download/plan.json", timeout=2) as response:  # noqa: S310
            assert json.load(response)["certificate"]["status"] == "proven_optimal"

        unconfirmed = Request(
            f"{base}/api/execution/start",
            data=json.dumps(proposal_input(app)).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error_info:
            urlopen(unconfirmed, timeout=2)  # noqa: S310
        assert error_info.value.code == 400
        started = post_json(
            f"{base}/api/execution/start",
            proposal_input(app, {"confirm_locked_acceptance": True}),
        )
        assert started["execution"]["active_count"] == 1
        assert post_json(f"{base}/api/execution/reset", {})["execution"] is None

        bad_host = Request(f"{base}/api/status", headers={"Host": "evil.example"})
        with pytest.raises(HTTPError) as error_info:
            urlopen(bad_host, timeout=2)  # noqa: S310
        assert error_info.value.code == 403

        malformed = Request(
            f"{base}/api/scan",
            data=b"{nope",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error_info:
            urlopen(malformed, timeout=2)  # noqa: S310
        body = json.loads(error_info.value.read())
        assert body == {"error": "request body is not valid JSON"}

        for path in ["/download/missing.json", "/does-not-exist"]:
            with pytest.raises(HTTPError) as error_info:
                urlopen(f"{base}{path}", timeout=2)  # noqa: S310
            assert error_info.value.code == 404

        unknown_post = Request(
            f"{base}/api/nope",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error_info:
            urlopen(unknown_post, timeout=2)  # noqa: S310
        assert error_info.value.code == 404

        wrong_type = Request(f"{base}/api/scan", data=b"{}", method="POST")
        with pytest.raises(HTTPError) as error_info:
            urlopen(wrong_type, timeout=2)  # noqa: S310
        assert error_info.value.code == 400

        list_body = Request(
            f"{base}/api/scan",
            data=b"[]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error_info:
            urlopen(list_body, timeout=2)  # noqa: S310
        assert "must be an object" in error_info.value.read().decode()

        bad_origin = Request(
            f"{base}/api/scan",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error_info:
            urlopen(bad_origin, timeout=2)  # noqa: S310
        assert error_info.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_web_subcommand_delegates_without_starting_server(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli_module, "load_bundled_graph", lambda: tiny_graph)

    def fake_run(
        graph: UniverseGraph,
        *,
        port: int,
        workspace: Path,
        open_browser: bool,
    ) -> int:
        captured.update(
            graph=graph,
            port=port,
            workspace=workspace,
            open_browser=open_browser,
        )
        return 0

    monkeypatch.setattr(cli_module, "run_local_web_ui", fake_run)
    assert (
        main(
            [
                "web",
                "--port",
                "9876",
                "--workspace",
                str(tmp_path),
                "--no-browser",
            ]
        )
        == 0
    )
    assert captured == {
        "graph": tiny_graph,
        "port": 9876,
        "workspace": tmp_path,
        "open_browser": False,
    }


def test_web_assets_are_packaged_as_external_csp_safe_resources() -> None:
    html = asset("index.html")[0].decode()
    css = asset("styles.css")[0].decode()
    javascript = "\n".join(
        asset(name)[0].decode() for name in ("app.js", "planner_form.js", "route_view.js")
    )
    assert '<script type="module" src="app.js"></script>' in html
    assert "Optimality is relative" in html
    assert "Use all SDE regions" in html
    assert "Match security bands" in html
    assert "NPC Empire" in html
    assert "Gate-by-gate itinerary" in html
    assert 'id="execution-lock-banner"' in html
    assert 'id="execution-top-pill"' in html
    assert "Resume current route" in html
    assert 'id="duration-minutes"' in html
    assert "Gate threat awareness" in html
    assert "0.5+ shown; raw ≥ 0.45" in html
    assert "Bound strengthening" in html
    assert "comma separated" not in html
    assert "security_bands" in javascript
    assert "threat_categories" in javascript
    assert "jump_path_systems" in javascript
    assert "system_relaxation_bound_isk" in javascript
    assert "hydratePlannerFromPlan" in javascript
    assert "Live execution restored." in javascript
    assert "Planning is locked while an execution session is active" in javascript
    assert "127.0.0.1" not in javascript  # API calls remain same-origin under the loopback server.
    assert "--cyan" in css
    assert ".execution-lock-banner" in css
    with pytest.raises(FileNotFoundError):
        asset("../pyproject.toml")


def test_web_workspace_platform_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "win"))
    assert default_workspace_path() == tmp_path / "win" / "EveCourierRouteOptimizer"

    monkeypatch.setattr(sys, "platform", "darwin")
    assert "Library/Application Support/EveCourierRouteOptimizer" in str(default_workspace_path())

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_workspace_path() == tmp_path / "xdg" / "eve-courier-route-optimizer"


def test_run_local_web_ui_lifecycle(
    tiny_graph: UniverseGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []

    class FakeServer:
        def serve_forever(self, poll_interval: float) -> None:
            events.append(("serve", poll_interval))
            raise KeyboardInterrupt

        def server_close(self) -> None:
            events.append("close")

    def create_server(app: PlanningWorkspace, port: int) -> FakeServer:
        return FakeServer()

    def open_browser(url: str) -> None:
        events.append(("open", url))

    monkeypatch.setattr(webapp_module, "create_http_server", create_server)
    monkeypatch.setattr(webbrowser, "open", open_browser)
    assert run_local_web_ui(tiny_graph, port=8765, workspace=tmp_path) == 0
    assert events == [("open", "http://127.0.0.1:8765/"), ("serve", 0.25), "close"]
    assert "local web UI" in capsys.readouterr().out
    with pytest.raises(ValueError, match="between 1 and 65535"):
        run_local_web_ui(tiny_graph, port=0, workspace=tmp_path, open_browser=False)
    with pytest.raises(ValueError, match="between 0 and 65535"):
        create_http_server(
            PlanningWorkspace(
                tiny_graph,
                EsiClient(transport=CourierTransport(datetime.now(UTC))),
                tmp_path / "bad-port",
            ),
            port=-1,
        )
