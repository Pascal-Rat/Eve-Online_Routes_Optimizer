from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest
from playwright.sync_api import Page, expect

from browser_tests.conftest import WebSession
from eve_courier_optimizer.jsonio import json_object
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.web import planning_payload, proposal_input, seed_snapshot


def test_two_tabs_cannot_apply_a_proposal_the_older_tab_never_reviewed(
    page: Page, web_session: WebSession
) -> None:
    app, clock = web_session.app, web_session.clock
    app.scan({"regions": [10]})
    app.solve(planning_payload())
    page.goto(web_session.url)
    expect(page.get_by_role("button", name="Arm this plan for execution")).to_be_enabled()
    second = page.context.new_page()
    try:
        seed_snapshot(
            app,
            make_snapshot(
                clock.value,
                make_contract(clock.value, 1, 101, 102),
                make_contract(clock.value, 2, 101, 102),
            ),
        )
        second.goto(web_session.url)
        second.get_by_label(re.compile("^Start system")).fill("Alpha")
        second.get_by_role("option", name=re.compile("Alpha")).first.click()
        second.get_by_role("button", name=re.compile("Optimize & prove"), exact=False).click()
        expect(second.get_by_role("button", name="Arm this plan for execution")).to_be_enabled(
            timeout=15000
        )
        page.locator("#locked-confirm").check()
        page.get_by_role("button", name="Arm this plan for execution").click()
        expect(page.locator("#notice")).to_contain_text("workspace changed")
        assert app.trip is None
        page.reload()
        page.locator("#locked-confirm").check()
        page.get_by_role("button", name="Arm this plan for execution").click()
        expect(page.locator("#exec-active")).to_have_text("2")
    finally:
        second.close()


@pytest.mark.parametrize("radius", [None, 0, 1000])
def test_saved_gate_radius_is_used_by_the_next_scan(
    page: Page, web_session: WebSession, radius: int | None
) -> None:
    app, clock = web_session.app, web_session.clock
    snapshot = make_snapshot(clock.value, make_contract(clock.value, 1, 101, 102))
    if radius is not None:
        snapshot = replace(
            snapshot,
            threat_intel_fetched_at=clock.value,
            threat_window_seconds=7200,
            threat_gate_radius_m=radius,
            threat_coverage_region_ids=(10,),
        )
    seed_snapshot(app, snapshot)
    solved = app.solve(
        {
            **planning_payload(),
            "gank_awareness": radius is not None,
            "threat_categories": ["any_gate_pvp"],
        }
    )
    assert solved["plan"]["model"]["threat_gate_radius_m"] == radius
    expected = "250" if radius is None else str(radius // 1000)
    page.goto(web_session.url)
    expect(page.locator("#route-wrap")).to_be_visible()
    expect(page.locator("#threat-gate-radius-km")).to_have_value(expected)
    page.route(
        "**/api/jobs",
        lambda route: route.fulfill(status=400, json={"error": "Request captured by test"}),
    )
    with page.expect_request("**/api/jobs") as request:
        page.locator("#scan-button").click()
    payload = json_object(request.value.post_data_json, "request")
    assert json_object(payload["input"], "input")["threat_gate_radius_km"] == expected


@pytest.mark.parametrize("failure", ["network", "http", "json", "shape"])
def test_status_errors_distinguish_offline_from_server_and_render_failures(
    page: Page, web_session: WebSession, failure: str
) -> None:
    from playwright.sync_api import Route

    def status(route: Route) -> None:
        if failure == "network":
            route.abort("connectionrefused")
        elif failure == "http":
            route.fulfill(status=500, json={"error": "Workspace storage is unavailable"})
        elif failure == "json":
            route.fulfill(status=200, content_type="application/json", body="{broken")
        else:
            route.fulfill(json={"revision": 1, "sde": None})

    page.route("**/api/status", status)
    page.goto(web_session.url)
    notice = page.locator("#notice")
    if failure == "network":
        expect(notice).to_contain_text("Local backend not connected")
    else:
        expect(notice).not_to_contain_text("Local backend not connected")
        expected = {
            "http": "Workspace storage is unavailable",
            "json": "invalid JSON",
            "shape": "is required",
        }[failure]
        expect(notice).to_contain_text(expected)


def test_planner_fields_have_meaningful_accessible_names(
    page: Page, web_session: WebSession
) -> None:
    page.goto(web_session.url)
    for name in (
        "Start system",
        "Collateral ISK",
        "Time budget hours",
        "Time budget minutes",
        "Avoid systems",
        "Required route systems",
        "Finish system",
    ):
        expect(page.get_by_label(re.compile("^" + name))).to_have_count(1)
    # Region selection is conditional, but its label must still resolve when the picker is shown.
    expect(page.get_by_label(re.compile("^Regions search"))).to_have_count(1)


def test_rolling_replan_requires_apply_before_using_the_refreshed_policy(
    page: Page, web_session: WebSession
) -> None:
    from tests.support.rolling_policy import prepare_rolling_policy_refresh

    app = web_session.app
    prepare_rolling_policy_refresh(app, web_session.clock.value)
    page.goto(web_session.url)
    expect(page.locator("#replan-button")).to_be_enabled()
    page.locator("#replan-button").click()
    apply = page.get_by_role("button", name="Apply revised plan to execution")
    expect(apply).to_be_visible(timeout=15000)
    expect(apply).to_be_enabled()
    expect(page.locator('#route-body button[data-action="pickup"]')).to_be_disabled()
    assert app.trip is not None and 2 in app.trip.security.threat_avoided_system_ids
    apply.click()
    pickup = page.locator('#route-body button[data-action="pickup"]')
    expect(pickup).to_be_enabled()
    assert app.trip is not None
    pickup.click()
    expect(page.locator("#exec-active")).to_have_text("1")
    assert app.trip is not None
    assert not app.trip.security.threat_avoided_system_ids
    assert app.trip.active_shipments[0].picked


def test_restoration_rejects_a_missing_required_policy_field(
    page: Page, web_session: WebSession
) -> None:
    app = web_session.app
    app.scan({"regions": [10]})
    app.solve(planning_payload())
    # First restore the complete real response, then corrupt just the required policy field.
    page.goto(web_session.url)
    expect(page.locator("#start-execution")).to_be_enabled()
    expect(page.locator("#threat-gate-radius-km")).to_have_value("250")
    payload = json_object(json.loads(json.dumps({**app.status(), "job": None})), "status")
    model = json_object(json_object(payload["plan"], "plan")["model"], "model")
    del model["threat_gate_radius_m"]
    page.route("**/api/status", lambda route: route.fulfill(json=payload))
    page.reload()
    expect(page.locator("#notice")).to_contain_text("does not match its declared type")
    expect(page.get_by_role("button", name="Arm this plan for execution")).to_be_hidden()
    expect(page.locator("#route-wrap")).to_be_hidden()


@pytest.mark.parametrize("operation", ["solve", "replan"])
@pytest.mark.parametrize("invalid", [False, True])
def test_completed_plan_job_validates_its_result_before_display(
    page: Page, web_session: WebSession, operation: str, invalid: bool
) -> None:
    app = web_session.app
    app.scan({"regions": [10]})
    result = app.solve(planning_payload())
    if operation == "replan":
        app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
        result = app.replan({"refresh": False})
    payload = json_object(json.loads(json.dumps(result)), "result")
    if invalid:
        model = json_object(json_object(payload["plan"], "plan")["model"], "model")
        model["start_system_id"] = []
    page.goto(web_session.url)
    button = page.locator(f"#{operation}-button")
    expect(button).to_be_enabled()
    page.route(
        "**/api/jobs",
        lambda route: route.fulfill(
            json={
                "job": {
                    "id": "contract-test-job",
                    "operation": operation,
                    "status": "completed",
                    "progress": "Complete",
                    "elapsed_seconds": 0,
                    "result": payload,
                }
            }
        ),
    )
    button.click()
    notice = page.locator("#notice")
    if invalid:
        expect(notice).to_contain_text(
            "PlanResponse.plan.model.start_system_id has an invalid primitive type"
        )
    else:
        expect(notice).to_contain_text(
            "Replan is ready but not armed yet"
            if operation == "replan"
            else "Global optimum proven"
        )
        expect(page.locator("#route-wrap")).to_be_visible()
