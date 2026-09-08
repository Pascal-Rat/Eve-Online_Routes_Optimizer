from __future__ import annotations

import json
from dataclasses import replace

import pytest
from playwright.sync_api import Page, expect

from eve_courier_optimizer.jsonio import json_object
from tests.browser.conftest import WebSession
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.web import planning_payload, seed_snapshot


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
